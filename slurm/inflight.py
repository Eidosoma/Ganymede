#!/usr/bin/env python3
"""inflight.py: the account's nodes in flight, from one squeue snapshot, for the fleet ceiling (submit.sh and fleet_dispatch.sh).

  SLURM_BITSTR_LEN=0 squeue -A ACCOUNT -h -t all -o '<letters joined by |>' | inflight.py --fields D,t,E,r,V,F,j,i \
      [--departing JOBID] [--reservations FILE] [--queue FLEET_QUEUE_FILE]

The field letters are squeue's format letters: A job id, D nodes, t state, E dependency, r reason, F array base id, j name, i job id
as shown (BASE_[2-9%4] for the pending part of an array); other letters are carried and ignored. Counted:
  - running and configuring jobs (each array element is its own row);
  - pending jobs that are free to start (no dependency, not held) and, with --departing, the jobs chained behind the leaving one;
  - pending jobs that are not held and wait on a dependency: such a job starts by itself the moment its predecessors end, on
    as many nodes as it asks for. Behind plain jobs that are in flight it counts only what it needs beyond the nodes they free (each
    such predecessor counts as the larger of its own nodes and the nodes of all the jobs chained behind it); behind anything else (an
    array, a pending job, a job not shown) it counts in full;
  - the pending part of an array is ONE row: nodes x the elements that may run at once, min(pending elements, throttle - elements
    of that array running), or every pending element when it has no throttle;
  - accepted jobs the snapshot does not show in any state, from reservation lines younger than --recent-s seconds
    (FLEET_RESERVATION_RECENT_S, 120; keep it below the site's MinJobAge) that were not submitted held or chained.
The leaving job (--departing) is not counted. The budget is a separate calculation (budget.py charges every pending element in full);
this one is how many nodes can be busy at the same time. squeue cuts a task list at 64 bytes unless SLURM_BITSTR_LEN=0: a list cut
short counts its throttle in full, and one with no throttle shown cannot be counted (exit 3), so the callers refuse or do nothing.

With --queue the first line is still the number, and each job of the fleet queue that is still held follows, in queue order, once:
'R|job|nodes|name' when it waits only for a node, 'D|job|nodes|name' when it still waits for a predecessor. A held job waits
for a predecessor while Slurm still shows a dependency for it that has not ended: Slurm keeps a held job's reason JobHeldUser and
updates its dependency list while it is held (slurmctld's periodic pass; src/slurmctld/job_mgr.c and job_scheduler.c, Slurm 25.05),
and a predecessor has ended when every row of it in the snapshot has ended or is the leaving job. A predecessor the snapshot does not
show, a failed dependency, and a kind other than afterok, afterany or afternotok keep the job waiting until Slurm clears them."""
import argparse, datetime as dt, os, re, sys

ARRAY = re.compile(r'^(\d+)_\[(.*)\]$')
CUT = re.compile(r'^(\d+)_\[')
PART = re.compile(r'(\d+)(?:-(\d+)(?::(\d+))?)?')
DEP_ITEM = re.compile(r'([a-z]+):([^()]+?)(?:\(([a-z]+)\))?')   # Slurm prints one 'kind:job(state)' per predecessor, joined by ',' or '?'
DEP_JOB = re.compile(r'(\d+)(?:_(\d+|\*))?')
ENDED = {'BF', 'CA', 'CD', 'CG', 'DL', 'F', 'NF', 'OOM', 'PR', 'RV', 'TO'}   # squeue's codes for a job that has ended (CG: ended, cleaning up)
KINDS = ('afterok', 'afterany', 'afternotok')   # ended predecessors decide these; others wait until Slurm clears them


def parse_spec(spec):
    """'0-3,5,7-11:2%4' -> (pending elements, or None when the list is cut short or unreadable; throttle, or None)."""
    body, pct, thr = spec.partition('%')
    limit = None
    if pct:
        if not thr.isdigit():
            return None, None
        limit = int(thr)
    n = 0
    for part in body.split(','):
        m = PART.fullmatch(part.strip())
        if not m:
            return None, limit
        a = int(m.group(1)); b = int(m.group(2)) if m.group(2) else a; s = int(m.group(3)) if m.group(3) else 1
        if b < a or s < 1:
            return None, limit
        n += (b - a) // s + 1
    return n, limit


def spec_has(spec, k):
    """Does the task list '0-3,5,7-11:2%4' hold element k? A list that cannot be read is taken to hold it."""
    for part in spec.partition('%')[0].split(','):
        m = PART.fullmatch(part.strip())
        if not m:
            return True
        a = int(m.group(1)); b = int(m.group(2)) if m.group(2) else a; s = int(m.group(3)) if m.group(3) else 1
        if a <= k <= b and (k - a) % max(s, 1) == 0:
            return True
    return False


def dep_items(s):
    """Slurm's dependency string -> [(kind, job, state)], [] when there is none, None when an item cannot be read (singleton, a
    '+minutes' offset, a form not known here). 'afterok:101:102' (the request's form) gives one item per job."""
    if s in ('', '(null)'):
        return []
    out = []
    for item in re.split(r'[,?]', s):
        m = DEP_ITEM.fullmatch(item.strip())
        if not m:
            return None
        for j in m.group(2).split(':'):
            if not DEP_JOB.fullmatch(j):
                return None
            out.append((m.group(1), j, m.group(3) or ''))
    return out


def split_row(line, names):
    """One squeue line into named fields; a job name holding '|' is put back together."""
    f = line.split('|')
    if len(f) < len(names):
        return None
    if len(f) > len(names):
        if 'j' not in names:
            return None
        k = names.index('j'); tail = len(names) - k - 1
        f = f[:k] + ['|'.join(f[k:len(f) - tail])] + f[len(f) - tail:]
    return dict(zip(names, f))


def queue_lines(path):
    """The fleet queue: 'UTC|job|name|nodes', and '|kind:job,..' after the nodes of a chained job -> (job, nodes, name)."""
    try:
        lines = open(path).read().splitlines()
    except FileNotFoundError:
        return []
    out, seen = [], set()
    for l in lines:
        f = l.split('|')
        if len(f) < 4:
            continue
        if len(f) >= 5 and not f[-1].isdigit():
            nodes, name = f[-2], '|'.join(f[2:-2])
        else:
            nodes, name = f[-1], '|'.join(f[2:-1])
        jid = f[1].split(';')[0]
        if jid and jid not in seen:
            seen.add(jid); out.append((jid, nodes, name))
    return out


def fail(msg):
    print('inflight: ' + msg, file=sys.stderr)
    return 3


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--fields', required=True)
    ap.add_argument('--departing', default='')
    ap.add_argument('--reservations', default='')
    ap.add_argument('--queue', default='')
    ap.add_argument('--recent-s', type=float, default=float(os.environ.get('FLEET_RESERVATION_RECENT_S') or 120))
    a = ap.parse_args()
    names = a.fields.split(',')
    rows = []
    for line in sys.stdin.read().splitlines():
        if not line.strip():
            continue
        r = split_row(line, names)
        if r is None:
            return fail('a queue line does not carry the fields ' + a.fields + ': ' + line[:120])
        rows.append(r)
    dep = a.departing

    def leaving(r):
        return dep != '' and dep in (r.get('A'), r.get('i'))

    def shown(r):
        return r.get('i') or r.get('A') or ''

    def base(r):
        return r.get('F') or shown(r).split('_')[0]

    def rows_of(jid):
        """The snapshot's rows of a predecessor: 'N' a plain job or a whole array, 'N_*' a whole array, 'N_K' one element (its own
        row, or the pending part of its array when the task list holds K)."""
        b, _, k = jid.partition('_')
        out = []
        for r in rows:
            if base(r) != b and r.get('A') != b:
                continue
            if k in ('', '*') or shown(r) == jid:
                out.append(r)
                continue
            m = ARRAY.match(shown(r))
            if (m and spec_has(m.group(2), int(k))) or (not m and CUT.match(shown(r))):
                out.append(r)
        return out

    def ended(kind, jid, state):
        if state == 'failed' or kind not in KINDS:
            return False
        rs = rows_of(jid)
        return bool(rs) and all(leaving(r) or r.get('t') in ENDED for r in rs)

    def nodes_of(r):
        try:
            return int(r.get('D') or '')
        except ValueError:
            return None

    running, seen, plain = {}, set(), {}
    for r in rows:
        b = base(r)
        seen.update(x for x in (b, shown(r).split('_')[0], r.get('A')) if x)
        if r.get('t') in ('R', 'CF') and not leaving(r):
            running[b] = running.get(b, 0) + 1
            if shown(r).isdigit() and nodes_of(r) is not None:
                plain[shown(r)] = nodes_of(r)   # a plain job in flight (not an array element): its nodes free when it ends
    n = 0
    behind = {}   # a plain job in flight -> the nodes of the unheld jobs chained behind it
    for r in rows:
        if leaving(r):
            continue
        state, depends = r.get('t'), r.get('E', '')
        attach = None
        if state not in ('R', 'CF'):
            if state != 'PD' or 'Held' in r.get('r', '') or r.get('r') == 'DependencyNeverSatisfied':
                continue   # held, or never to start (a failed dependency Slurm keeps pending)
            free = depends in ('', '(null)')
            chained = dep != '' and re.search(r'(^|[:,?])' + re.escape(dep) + r'([(:,?_]|$)', depends) is not None
            if not (free or chained):
                # waiting on a dependency, not held: it starts by itself when its predecessors end (left uncounted, a larger
                # successor, or two behind one job, could take more nodes than their predecessor freed)
                items = dep_items(depends)
                if items and shown(r).isdigit() and all(k in KINDS and s != 'failed' and j in plain for k, j, s in items):
                    attach = sorted(set(j for _, j, _ in items))
        nodes = nodes_of(r)
        if nodes is None:
            return fail('a node count is not a number: ' + repr(r.get('D')))
        m = ARRAY.match(shown(r)) if state == 'PD' else None
        if m:
            pending, limit = parse_spec(m.group(2))
            if limit is None:
                if pending is None:
                    return fail('array ' + m.group(1) + ': its task list cannot be read and no throttle is shown (' + shown(r)[:80] + ')')
                k = pending
            else:
                avail = max(0, limit - running.get(m.group(1), 0))
                k = avail if pending is None else min(pending, avail)
            count = nodes * k
        elif state == 'PD' and CUT.match(shown(r)):
            return fail('array ' + shown(r)[:60] + ': its task list was cut short (squeue needs SLURM_BITSTR_LEN=0)')
        else:
            count = nodes
        if attach:
            for j in attach:
                behind[j] = behind.get(j, 0) + count
        else:
            n += count
    n += sum(max(0, s - plain[j]) for j, s in behind.items())   # each predecessor: the larger of its nodes and its successors'
    if a.reservations:
        try:
            lines = open(a.reservations).read().splitlines()
        except FileNotFoundError:
            lines = []
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        for l in lines:
            f = l.split('|')
            if len(f) < 8 or f[7].strip() == '1':
                continue   # an older line carries no node count; a job submitted held or chained does not start by itself
            jid = f[1].split(';')[0]
            if jid.split('_')[0] in seen or (dep != '' and jid == dep):
                continue   # the snapshot shows it, in whatever state: it is counted above, or it has ended
            try:
                age = (now - dt.datetime.strptime(f[0], '%Y-%m-%dT%H:%M:%SZ')).total_seconds()
            except ValueError:
                continue
            if age < a.recent_s:
                try:
                    n += int(f[6])
                except ValueError:
                    n += 1
    print(n)
    if a.queue:
        for jid, qnodes, name in queue_lines(a.queue):
            held = [r for r in rows if base(r) == jid and r.get('t') == 'PD' and 'JobHeldUser' in r.get('r', '')]
            if not held:
                continue   # released, running, cancelled or ended: the dispatcher prunes the line
            depends = held[0].get('E', '')
            items = dep_items(depends)
            ready = depends in ('', '(null)') or (items is not None and all(ended(k, j, s) for k, j, s in items))
            print(('R|' if ready else 'D|') + jid + '|' + qnodes + '|' + name)
    return 0


if __name__ == '__main__':
    sys.exit(main())
