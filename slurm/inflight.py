#!/usr/bin/env python3
"""inflight.py: the account's nodes in flight, from one squeue snapshot, for the fleet ceiling (submit.sh and fleet_dispatch.sh).

  SLURM_BITSTR_LEN=0 squeue -A ACCOUNT -h -t all -o '<letters joined by |>' | inflight.py --fields D,t,E,r,V,F,j,i \
      [--departing JOBID] [--reservations FILE]

The field letters are squeue's format letters: A job id, D nodes, t state, E dependency, r reason, F array base id, j name, i job id
as shown (BASE_[2-9%4] for the pending part of an array); other letters are carried and ignored. Counted:
  - running and configuring jobs (each array element is its own row);
  - pending jobs that are free to start (no dependency, not held) and, with --departing, the jobs chained behind the leaving one;
  - the pending part of an array is ONE row: nodes x the elements that may run at once, min(pending elements, throttle - elements
    of that array running), or every pending element when it has no throttle;
  - accepted jobs the snapshot does not show in any state, from reservation lines younger than --recent-s seconds
    (FLEET_RESERVATION_RECENT_S, 120; keep it below the site's MinJobAge) that were not submitted held or chained.
The leaving job (--departing) is not counted. The budget is a separate calculation (budget.py charges every pending element in full);
this one is how many nodes can be busy at the same time. squeue cuts a task list at 64 bytes unless SLURM_BITSTR_LEN=0: a list cut
short counts its throttle in full, and one with no throttle shown cannot be counted (exit 3), so the callers refuse or do nothing."""
import argparse, datetime as dt, os, re, sys

ARRAY = re.compile(r'^(\d+)_\[(.*)\]$')
CUT = re.compile(r'^(\d+)_\[')
PART = re.compile(r'(\d+)(?:-(\d+)(?::(\d+))?)?')


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


def fail(msg):
    print('inflight: ' + msg, file=sys.stderr)
    return 3


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--fields', required=True)
    ap.add_argument('--departing', default='')
    ap.add_argument('--reservations', default='')
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

    running, seen = {}, set()
    for r in rows:
        shown = r.get('i') or r.get('A') or ''
        base = r.get('F') or shown.split('_')[0]
        seen.update(x for x in (base, shown.split('_')[0], r.get('A')) if x)
        if r.get('t') in ('R', 'CF') and not leaving(r):
            running[base] = running.get(base, 0) + 1
    n = 0
    for r in rows:
        if leaving(r):
            continue
        state, depends = r.get('t'), r.get('E', '')
        if state not in ('R', 'CF'):
            if state != 'PD' or 'Held' in r.get('r', ''):
                continue
            free = depends in ('', '(null)')
            chained = dep != '' and re.search(r'(^|[:,?])' + re.escape(dep) + r'([(:,?_]|$)', depends) is not None
            if not (free or chained):
                continue
        try:
            nodes = int(r.get('D') or '')
        except ValueError:
            return fail('a node count is not a number: ' + repr(r.get('D')))
        shown = r.get('i', '')
        m = ARRAY.match(shown) if state == 'PD' else None
        if m:
            pending, limit = parse_spec(m.group(2))
            if limit is None:
                if pending is None:
                    return fail('array ' + m.group(1) + ': its task list cannot be read and no throttle is shown (' + shown[:80] + ')')
                k = pending
            else:
                avail = max(0, limit - running.get(m.group(1), 0))
                k = avail if pending is None else min(pending, avail)
            n += nodes * k
        elif state == 'PD' and CUT.match(shown):
            return fail('array ' + shown[:60] + ': its task list was cut short (squeue needs SLURM_BITSTR_LEN=0)')
        else:
            n += nodes
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
    return 0


if __name__ == '__main__':
    sys.exit(main())
