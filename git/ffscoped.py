#!/usr/bin/env python3
"""ffscoped.py: fast-forward a shared checkout's local main to origin/main, touching only the paths that differ.

For fleets of agents that all commit on main in ONE checkout (see lanegit.sh). Every lane push lands on origin as a merge commit, and
people may push from outside, so local main trails origin. An operator levels it with this tool; lanes never pull or merge.

Why not `git merge --ff-only`: before merging, git lstat's every tracked file. On a 320,000-file checkout on a parallel file system
that took 15 minutes, and an agent that committed in that window made git refuse its final ref update after the files and the index
were already written, leaving the checkout split between two states.

The route, each step guarded:
  1. fetch origin main; local main must be an ancestor of origin/main (a lane's unpushed commit: wait up to two minutes for its push,
     then refuse).
  2. for every path that differs between main and origin/main, the working file and the index entry must each equal main's version or
     origin's (origin's = already applied by an earlier round); anything else is a lane's edit: refuse and list. Only these paths are
     read: no whole-tree scan.
  3. the index entries of those paths are set to origin's (update-index --index-info, which takes the lock and reads the index under
     it), the files written from the index (checkout-index -f -u: modes and symlinks right, stat info fresh), files origin deleted
     removed with their emptied folders. Every other index entry, a lane's staged work included, is left alone.
  4. `update-ref refs/heads/main <origin> <old main>`: refused if a lane moved main meanwhile; then fetch again (the lane's push lands
     on origin within a minute) and repeat; every round is idempotent. At most three ref attempts.
Exit codes: 0 done or nothing to do; 2 usage, not a checkout, or HEAD not on main; 3 local main has commits origin lacks; 4 index.lock
held; 5 a lane's edit in the way; 6 unsupported entry (submodule, path with a newline); 8 git failure; 10 main kept moving.

  python3 git/ffscoped.py [--dry-run]          (run it from the shared checkout, or set FF_ROOT)
Environment: FF_ROOT (the checkout; default: the one you are in), FF_REMOTE (default origin), FLEET_GIT_TOKEN_FILE (optional token
file for HTTPS, read only by git's credential helper and never printed). Tests only: FF_TEST_BEFORE_REF (a shell command run once just
before the first ref update), FF_WAIT_S / FF_WAIT_TRIES (waiting for a lane's push), FF_LOCK_WAIT_S, FF_SKIP_FETCH.
"""
import os, sys, subprocess, time, stat


def _toplevel():
    try:
        return subprocess.run(['git', 'rev-parse', '--show-toplevel'], capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return ''



ROOT = os.environ.get('FF_ROOT') or _toplevel()
REMOTE = os.environ.get('FF_REMOTE', 'origin')
TOKEN = os.environ.get('FLEET_GIT_TOKEN_FILE', '')   # optional: a token file for HTTPS, read only by git's credential helper
CRED = '!f() { echo username=x-access-token; echo "password=$(cat ' + TOKEN + ')"; }; f'
BASE = ['git', '-c', 'core.preloadIndex=false', '-c', 'gc.auto=0', '-c', 'maintenance.auto=false']
WAIT_S = float(os.environ.get('FF_WAIT_S', '30'))
WAIT_TRIES = int(os.environ.get('FF_WAIT_TRIES', '4'))
LOCK_WAIT_S = float(os.environ.get('FF_LOCK_WAIT_S', '120'))
ZERO = '0' * 40
HOOK_DONE = []
ENV = dict(os.environ)
ENV.pop('GIT_OPTIONAL_LOCKS', None)   # never around an index write: it let a long-running git write back a stale index
ENV['LC_ALL'] = 'C'


def stamp():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def say(msg):
    print(stamp() + ' ' + msg, flush=True)


def clean(s):
    return '\n'.join(l for l in s.splitlines() if not any(k in l.lower() for k in ('password', 'ghp_', 'github_pat')))


class GitError(Exception):
    pass


def g(*args, inp=None, check=True, raw=False):
    r = subprocess.run(BASE + list(args), cwd=ROOT, env=ENV, input=inp, capture_output=True)
    if check and r.returncode != 0:
        raise GitError('git ' + ' '.join(args[:3]) + ' failed (' + str(r.returncode) + '): ' + clean(r.stderr.decode(errors='replace'))[:400])
    return r if raw else r.stdout.decode(errors='surrogateescape')


def lock_path():
    gd = g('rev-parse', '--git-dir').strip()
    return gd if os.path.isabs(gd) else os.path.join(ROOT, gd)


def wait_lock(gitdir):
    t0 = time.time()
    while os.path.exists(os.path.join(gitdir, 'index.lock')):
        if time.time() - t0 > LOCK_WAIT_S:
            return False
        time.sleep(2)
    return True


def with_lock_retry(gitdir, *args, inp=None):
    for attempt in range(8):
        if not wait_lock(gitdir):
            raise GitError('index.lock held over ' + str(int(LOCK_WAIT_S)) + ' s')
        r = g(*args, inp=inp, check=False, raw=True)
        if r.returncode == 0:
            return r
        err = r.stderr.decode(errors='replace')
        if 'index.lock' in err or 'Unable to create' in err:
            time.sleep(3)
            continue
        raise GitError('git ' + ' '.join(args[:2]) + ' failed: ' + clean(err)[:400])
    raise GitError('index.lock contention eight times')


def raw_rows(out):
    """Parse -z raw diff output (:oldmode newmode oldsha newsha status NUL path NUL) into a dict path -> tuple."""
    parts = out.split('\0')
    rows = {}
    i = 0
    while i < len(parts) - 1:
        meta = parts[i]
        if not meta.startswith(':'):
            i += 1
            continue
        om, nm, osha, nsha, st = meta[1:].split(' ')
        path = parts[i + 1]
        rows[path] = (om, nm, osha, nsha, st[0])
        i += 2
    return rows


def work_state(paths, filemode):
    """(mode, sha) of each working path, None if absent; 'DIR' if a folder sits there; 'BLOCKED' if a parent is a file."""
    res, regular = {}, []
    for p in paths:
        full = os.path.join(ROOT, p)
        parent, blocked = os.path.dirname(p), False
        while parent:
            fp = os.path.join(ROOT, parent)
            if os.path.lexists(fp) and not os.path.isdir(fp):
                blocked = True
                break
            parent = os.path.dirname(parent)
        if blocked:
            res[p] = 'BLOCKED'
            continue
        try:
            stt = os.lstat(full)
        except FileNotFoundError:
            res[p] = None
            continue
        if stat.S_ISDIR(stt.st_mode):
            res[p] = 'DIR'
        elif stat.S_ISLNK(stt.st_mode):
            target = os.readlink(full).encode(errors='surrogateescape')
            res[p] = ('120000', g('hash-object', '--stdin', inp=target).strip())
        else:
            mode = '100755' if (filemode and stt.st_mode & 0o100) else '100644'
            res[p] = (mode, None)
            regular.append(p)
    if regular:
        out = g('hash-object', '--stdin-paths', inp=('\n'.join(regular) + '\n').encode(errors='surrogateescape')).split('\n')
        for p, sha in zip(regular, out):
            res[p] = (res[p][0], sha.strip())
    return res


def same_mode(a, b, filemode):
    if a == b:
        return True
    return (not filemode) and {a, b} <= {'100644', '100755'}


def one_round(rnd, dry, gitdir, filemode):
    if os.environ.get('FF_SKIP_FETCH') != '1':
        g(*(['-c', 'credential.helper=', '-c', 'credential.helper=' + CRED] if TOKEN else []), 'fetch', '-q', REMOTE,
          '+refs/heads/main:refs/remotes/' + REMOTE + '/main')
    H = g('rev-parse', 'HEAD').strip()
    O = g('rev-parse', 'refs/remotes/' + REMOTE + '/main').strip()
    say('round ' + str(rnd) + ': main ' + H[:10] + ', ' + REMOTE + '/main ' + O[:10])
    if H == O:
        return 'level', H, O, 0
    if g('merge-base', '--is-ancestor', H, O, check=False, raw=True).returncode != 0:
        ahead = g('log', '--format=  %h %s', O + '..' + H).rstrip()
        return 'ahead', H, O, ahead
    behind = int(g('rev-list', '--count', H + '..' + O).strip())
    rows = raw_rows(g('diff-tree', '-r', '-z', '--no-renames', H, O))
    if any('\n' in p for p in rows):
        return 'unsupported', H, O, 'a path with a newline'
    if any(r[0] == '160000' or r[1] == '160000' for r in rows.values()):
        return 'unsupported', H, O, 'a submodule entry'
    staged = raw_rows(g('diff-index', '--cached', '-r', '-z', '--no-renames', H))   # index vs main, no stat
    work = work_state(list(rows), filemode)
    conflicts, set_index, write_work, remove_work = [], [], [], []
    for p, (om, nm, osha, nsha, st) in sorted(rows.items()):
        h_ent = None if st == 'A' else (om, osha)
        o_ent = None if st == 'D' else (nm, nsha)
        if p in staged:
            s = staged[p]
            i_ent = None if s[1] == '000000' else (s[1], s[3])
        else:
            i_ent = h_ent
        w = work[p]
        if w in ('DIR', 'BLOCKED'):
            conflicts.append(('a folder in the way' if w == 'DIR' else 'a file in the way of its folder') + ': ' + p)
            continue
        def eq(a, b):
            if a is None or b is None:
                return a is None and b is None
            return a[1] == b[1] and same_mode(a[0], b[0], filemode)
        w_h, w_o = eq(w, h_ent), eq(w, o_ent)
        i_h, i_o = eq(i_ent, h_ent), eq(i_ent, o_ent)
        if not (w_h or w_o):
            conflicts.append(('untracked file in the way' if st == 'A' else 'edited locally') + ': ' + p)
        if not (i_h or i_o):
            conflicts.append('staged locally: ' + p)
        if not (w_h or w_o) or not (i_h or i_o):
            continue
        if not i_o:
            set_index.append((p, o_ent))
        if st == 'D':
            if w is not None:
                remove_work.append(p)
        elif not w_o or not i_o:
            write_work.append(p)
    say('round ' + str(rnd) + ': ' + str(behind) + ' commit(s), ' + str(len(rows)) + ' path(s) differ ('
        + str(sum(1 for r in rows.values() if r[4] == 'A')) + ' added, '
        + str(sum(1 for r in rows.values() if r[4] == 'D')) + ' deleted, '
        + str(sum(1 for r in rows.values() if r[4] not in 'AD')) + ' modified); to apply: index '
        + str(len(set_index)) + ', files ' + str(len(write_work)) + ', removals ' + str(len(remove_work))
        + '; conflicts ' + str(len(conflicts)))
    if conflicts:
        return 'conflict', H, O, conflicts
    if dry:
        return 'dry', H, O, behind
    if set_index:
        lines = []
        for p, ent in set_index:
            if ent is None:
                lines.append('0 ' + ZERO + '\t' + p)
            else:
                lines.append(ent[0] + ' ' + ent[1] + '\t' + p)
        with_lock_retry(gitdir, 'update-index', '-z', '--index-info', inp=('\0'.join(lines) + '\0').encode(errors='surrogateescape'))
    if write_work:
        with_lock_retry(gitdir, 'checkout-index', '-f', '-u', '-z', '--stdin', inp=('\0'.join(write_work) + '\0').encode(errors='surrogateescape'))
    for p in remove_work:
        full = os.path.join(ROOT, p)
        try:
            os.unlink(full)
        except FileNotFoundError:
            pass
        d = os.path.dirname(p)
        while d:
            try:
                os.rmdir(os.path.join(ROOT, d))
            except OSError:
                break
            d = os.path.dirname(d)
    staged2 = raw_rows(g('diff-index', '--cached', '-r', '-z', '--no-renames', O))
    left = [p for p in rows if p in staged2]
    if left:
        return 'index', H, O, left
    hook = os.environ.get('FF_TEST_BEFORE_REF')
    if hook and not HOOK_DONE:
        HOOK_DONE.append(1)
        subprocess.run(['bash', '-c', hook], cwd=ROOT, env=ENV)
    r = g('update-ref', '-m', 'ffscoped: main to ' + REMOTE + '/main', 'refs/heads/main', O, H, check=False, raw=True)
    if r.returncode != 0:
        return 'moved', H, O, clean(r.stderr.decode(errors='replace')).strip()
    return 'done', H, O, behind


def main():
    args = sys.argv[1:]
    if any(a not in ('--dry-run',) for a in args):
        print(__doc__)
        return 2
    dry = '--dry-run' in args
    if not ROOT:
        say('REFUSED: not inside a git checkout; run it from the shared checkout or set FF_ROOT')
        return 2
    t0 = time.time()
    try:
        if g('symbolic-ref', '-q', 'HEAD', check=False).strip() != 'refs/heads/main':
            say('REFUSED: HEAD is not on main')
            return 2
        gitdir = lock_path()
        filemode = g('config', '--bool', 'core.fileMode', check=False).strip() != 'false'
        if not wait_lock(gitdir):
            say('REFUSED: index.lock held over ' + str(int(LOCK_WAIT_S)) + ' s; nothing changed; try again')
            return 4
        say('ffscoped start in ' + ROOT + (' (dry run)' if dry else ''))
        waits, moves, rnd = 0, 0, 0
        while True:
            rnd += 1
            res, H, O, info = one_round(rnd, dry, gitdir, filemode)
            if res == 'level':
                say(('nothing to do: main is at ' if rnd == 1 else 'DONE: main is at ') + REMOTE + '/main ' + O[:10])
                return 0
            if res == 'done':
                say('DONE: main ' + H[:10] + ' -> ' + O[:10] + ' (' + str(info) + ' commit(s)) in '
                    + str(round(time.time() - t0, 1)) + ' s')
                return 0
            if res == 'dry':
                say('DRY RUN OK: nothing changed; a fast-forward of ' + str(info) + ' commit(s) would proceed')
                return 0
            if res == 'conflict':
                for c in info[:50]:
                    print('  ' + c)
                if len(info) > 50:
                    print('  ... and ' + str(len(info) - 50) + ' more')
                say('REFUSED: ' + str(len(info)) + ' path(s) in the way' + ('' if rnd == 1 else ' (earlier rounds applied origin\'s files; nothing of a lane\'s was touched)'))
                return 5
            if res == 'unsupported':
                say('REFUSED: ' + info + ' in the update; not handled here')
                return 6
            if res == 'index':
                say('STOP: index entries still differ from origin after the update: ' + ', '.join(info[:10]))
                return 8
            if res == 'ahead':
                if dry or waits >= WAIT_TRIES:
                    print(info)
                    say('REFUSED: local main has commit(s) origin lacks (a lane\'s commit not yet pushed); it pushes with '
                        'lanegit.sh push, then run again' + ('; the changed paths are already at origin\'s version, a rerun finishes' if moves else '; nothing changed'))
                    return 3
                waits += 1
                say('local main has a lane\'s commit not yet on origin; waiting ' + str(int(WAIT_S)) + ' s for its push (' + str(waits) + ' of ' + str(WAIT_TRIES) + ')')
                time.sleep(WAIT_S)
                continue
            if res == 'moved':
                moves += 1
                last = (info.splitlines() or ['refused'])[-1][:160]
                if moves >= 3:
                    say('STOP: main moved in 3 rounds (' + last + '); the changed paths are at origin\'s version; run again')
                    return 10
                say('main moved during round ' + str(rnd) + ' (a lane committed): ' + last + '; again')
                continue
            say('STOP: unexpected state ' + res)
            return 8
    except GitError as e:
        say('STOP: ' + str(e))
        return 8


if __name__ == '__main__':
    sys.exit(main())
