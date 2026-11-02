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
     origin's (origin's = already applied by an earlier round); anything else is a lane's edit: refuse and list. A file that is to be
     replaced or removed must also have been quiet for FF_SETTLE_S seconds (60): a newer one means its writer may still be at work, so
     the tool refuses before writing anything. Only these paths are read: no whole-tree scan.
  3. origin's versions are written into a staging folder in .git (checkout-index from a temporary index holding only those paths:
     modes, symlinks and filters as git writes them; the shared index is not touched yet). Then one path at a time: the working file is
     moved into this run's backup folder (.git/ffscoped-backup/<UTC>-<pid>-<round>/files/) and checked again; if it changed after step
     2 it is put back and the round stops. Origin's version is then put in place through a hard link (or a new symlink), which fails
     rather than replace a file that a writer created in the meantime: the round stops and leaves that file as it is. A file origin
     deleted is moved aside the same way, and its emptied folders are removed.
  4. the index entries of the paths now at origin's version are set to origin's (update-index --index-info, which takes the lock and
     reads the index under it) and their stat information refreshed (add --refresh on those paths only). Every other index entry, a
     lane's staged work included, is left alone. The moved-aside files are checked once more: a write that landed in one after the move
     (a writer that had the file open) stops the round, and the file is kept.
  5. `update-ref refs/heads/main <origin> <old main>`: refused if a lane moved main meanwhile; then fetch again (the lane's push lands
     on origin within a minute) and repeat; every round is idempotent. At most three ref attempts.
Writers: pause writes to the paths that the dry run lists while the tool runs. The tool never overwrites or deletes a write: one made
after its check is put back, or kept in the backup folder, and the round stops before main moves (exit 11). One case only the pause
closes: a writer that opened a file before the update and writes into it afterwards writes into the moved-aside copy, which is kept,
and named if the write comes before the end of the run. Backups are kept FF_KEEP_DAYS (7) days; an older one whose files still match
what was moved aside is removed at the next run, any other is kept and named.
Exit codes: 0 done or nothing to do; 2 usage, not a checkout, or HEAD not on main; 3 local main has commits origin lacks; 4 index.lock
held; 5 a lane's edit in the way, or a file written in the last FF_SETTLE_S seconds (nothing changed); 6 unsupported entry (submodule,
path with a newline); 8 git failure; 10 main kept moving; 11 a write during the run (the round stopped, main not moved, the write kept).

  python3 git/ffscoped.py [--dry-run]          (run it from the shared checkout, or set FF_ROOT)
Environment: FF_ROOT (the checkout; default: the one you are in), FF_REMOTE (default origin), FLEET_GIT_TOKEN_FILE (optional token
file for HTTPS, read only by git's credential helper and never printed), FF_SETTLE_S (60), FF_KEEP_DAYS (7). Tests only:
FF_TEST_BEFORE_REF (a shell command run once just before the first ref update), FF_TEST_AFTER_ASIDE (a shell command run once after
the first file is moved aside and checked, with FF_ASIDE naming the moved file), FF_WAIT_S / FF_WAIT_TRIES (waiting for a lane's
push), FF_LOCK_WAIT_S, FF_SKIP_FETCH.
"""
import os, sys, subprocess, time, stat
import calendar, errno, json, shutil


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
SETTLE_S = float(os.environ.get('FF_SETTLE_S', '60'))
KEEP_DAYS = float(os.environ.get('FF_KEEP_DAYS', '7'))
ZERO = '0' * 40
HOOK_DONE = []
ASIDE_HOOK_DONE = []
COUNTER = [0]
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


def g(*args, inp=None, check=True, raw=False, env=None):
    r = subprocess.run(BASE + list(args), cwd=ROOT, env=env or ENV, input=inp, capture_output=True)
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


def with_lock_retry(gitdir, *args, inp=None, env=None):
    for attempt in range(8):
        if not wait_lock(gitdir):
            raise GitError('index.lock held over ' + str(int(LOCK_WAIT_S)) + ' s')
        r = g(*args, inp=inp, check=False, raw=True, env=env)
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


def eq(a, b, filemode):
    """The same (mode, sha) state? None = nothing there; 'DIR' and 'BLOCKED' equal nothing."""
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, str) or isinstance(b, str):
        return False
    return a[1] == b[1] and same_mode(a[0], b[0], filemode)


def state_at(loc, p, filemode):
    """(mode, sha) of whatever is at loc, hashed as git hashes the working file p (its filters); None if nothing is there."""
    try:
        stt = os.lstat(loc)
    except FileNotFoundError:
        return None
    if stat.S_ISDIR(stt.st_mode):
        return 'DIR'
    if stat.S_ISLNK(stt.st_mode):
        return ('120000', g('hash-object', '--stdin', inp=os.readlink(loc).encode(errors='surrogateescape')).strip())
    mode = '100755' if (filemode and stt.st_mode & 0o100) else '100644'
    return (mode, g('hash-object', '--path=' + p, '--', loc).strip())


def unique(d, what):
    COUNTER[0] += 1
    return os.path.join(d, '.ffscoped-' + what + '-' + str(os.getpid()) + '-' + str(COUNTER[0]))


def move_aside(full, run_dir, p):
    """Move the working file into this run's backup folder: one rename, so whatever a writer did up to now moves with it.
    Beside the file, hidden, if the backup folder is on another file system. Returns where it went."""
    dest = os.path.join(run_dir, 'files', p)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        os.rename(full, dest)
        return dest
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
    dest = unique(os.path.dirname(full), 'aside')
    os.rename(full, dest)
    return dest


def copy_new(src, dst):
    """Create dst, which must not exist (O_EXCL), with src's content; mode as a checkout creates it, in dst's own folder."""
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o777 if os.stat(src).st_mode & 0o100 else 0o666)
    with os.fdopen(fd, 'wb') as out, open(src, 'rb') as f:
        shutil.copyfileobj(f, out)


def put_back(loc, full):
    """Return a moved file to its place, never over something that appeared there meanwhile. True if it is back."""
    try:
        if os.path.islink(loc):
            os.symlink(os.readlink(loc), full)
        else:
            os.link(loc, full)
    except FileExistsError:
        return False
    except OSError:          # no hard link possible: a copy, created only if nothing is there; the moved file stays kept
        try:
            copy_new(loc, full)
            return True
        except FileExistsError:
            return False
    os.unlink(loc)
    return True


def put_new(staged, full):
    """Put origin's version (staged) at full only if nothing is there: copied to a hidden file in the target's own folder,
    then hard-linked to its name, which fails rather than replace a file a writer created meanwhile. True if placed."""
    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
    except (FileExistsError, NotADirectoryError):
        return False
    if os.path.islink(staged):
        try:
            os.symlink(os.readlink(staged), full)
            return True
        except FileExistsError:
            return False
    tmp = unique(os.path.dirname(full), 'new')
    copy_new(staged, tmp)
    try:
        os.link(tmp, full)
        return True
    except FileExistsError:
        return False
    except OSError:          # no hard links on this file system: create it in place, still never over an existing file
        try:
            copy_new(staged, full)
            return True
        except FileExistsError:
            return False
    finally:
        os.unlink(tmp)


def materialize(gitdir, entries, stage):
    """Origin's versions of these paths, written under stage as git's checkout writes them (modes, symlinks, filters), from a
    temporary index that holds only them: the shared index is not touched."""
    shutil.rmtree(stage, ignore_errors=True)
    tmp = os.path.join(gitdir, 'ffscoped-index-' + str(os.getpid()))
    env = dict(ENV, GIT_INDEX_FILE=tmp)
    try:
        g('update-index', '-z', '--index-info', env=env,
          inp=''.join(e[0] + ' ' + e[1] + '\t' + p + '\0' for p, e in entries).encode(errors='surrogateescape'))
        g('checkout-index', '-a', '-f', '--prefix=' + stage + '/', env=env)
    finally:
        for f in (tmp, tmp + '.lock'):
            try:
                os.unlink(f)
            except FileNotFoundError:
                pass


def note_kept(run_dir, p, loc, exp):
    """One line in the run's manifest for each moved-aside file: the path, where it is, and what was moved aside."""
    os.makedirs(run_dir, exist_ok=True)
    rel = os.path.relpath(loc, run_dir) if loc.startswith(run_dir + os.sep) else loc
    with open(os.path.join(run_dir, 'MANIFEST.jsonl'), 'a', encoding='utf-8', errors='surrogateescape') as f:
        f.write(json.dumps({'path': p, 'loc': rel, 'mode': exp[0], 'sha': exp[1]}) + '\n')


def prune_backups(gitdir, filemode):
    """Remove the backups older than FF_KEEP_DAYS whose files still match what was moved aside; name the others."""
    root = os.path.join(gitdir, 'ffscoped-backup')
    try:
        runs = sorted(os.listdir(root))
    except FileNotFoundError:
        return
    for d in runs:
        run_dir = os.path.join(root, d)
        try:
            t = calendar.timegm(time.strptime(d.split('-')[0], '%Y%m%dT%H%M%SZ'))
        except ValueError:
            continue
        if time.time() - t < KEEP_DAYS * 86400:
            continue
        man = os.path.join(run_dir, 'MANIFEST.jsonl')
        try:
            entries = [json.loads(l) for l in open(man, encoding='utf-8', errors='surrogateescape') if l.strip()]
        except (OSError, ValueError):
            entries = None
        locs, same = [], entries is not None
        for e in entries or []:
            loc = e['loc'] if os.path.isabs(e['loc']) else os.path.join(run_dir, e['loc'])
            now = state_at(loc, e['path'], filemode)
            if now is not None and not eq(now, (e['mode'], e['sha']), filemode):
                same = False
            locs.append(loc)
        if not same:
            say('NOTE: backup ' + run_dir + ' kept: a file in it differs from what was moved aside, or its list is missing '
                '(a writer may have written into it); look at it before removing it')
            continue
        for loc in locs:
            if not loc.startswith(run_dir + os.sep):
                try:
                    os.unlink(loc)
                except FileNotFoundError:
                    pass
        shutil.rmtree(run_dir, ignore_errors=True)
        say('removed the backup ' + d + ' (older than ' + str(KEEP_DAYS).rstrip('0').rstrip('.') + ' days, unchanged)')


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
    conflicts, set_index, write_work, remove_work, o_ent_of = [], [], [], [], {}
    now = time.time()
    for p, (om, nm, osha, nsha, st) in sorted(rows.items()):
        h_ent = None if st == 'A' else (om, osha)
        o_ent = None if st == 'D' else (nm, nsha)
        o_ent_of[p] = o_ent
        if p in staged:
            s = staged[p]
            i_ent = None if s[1] == '000000' else (s[1], s[3])
        else:
            i_ent = h_ent
        w = work[p]
        if w in ('DIR', 'BLOCKED'):
            conflicts.append(('a folder in the way' if w == 'DIR' else 'a file in the way of its folder') + ': ' + p)
            continue
        w_h, w_o = eq(w, h_ent, filemode), eq(w, o_ent, filemode)
        i_h, i_o = eq(i_ent, h_ent, filemode), eq(i_ent, o_ent, filemode)
        if not (w_h or w_o):
            conflicts.append(('untracked file in the way' if st == 'A' else 'edited locally') + ': ' + p)
        if not (i_h or i_o):
            conflicts.append('staged locally: ' + p)
        if not (w_h or w_o) or not (i_h or i_o):
            continue
        if w_o:                                  # the working file is origin's version already: at most its index entry
            if not i_o:
                set_index.append((p, o_ent))
            continue
        if w is not None and SETTLE_S > 0:       # it will be replaced or removed: it must have been quiet for a while
            try:
                age = now - os.lstat(os.path.join(ROOT, p)).st_mtime
            except FileNotFoundError:
                age = SETTLE_S
            if age < SETTLE_S:
                conflicts.append('written ' + str(max(0, int(age))) + ' s ago, within FF_SETTLE_S=' + str(int(SETTLE_S))
                                 + ' (its writer may be at work: pause it, then run again): ' + p)
                continue
        if not i_o:
            set_index.append((p, o_ent))
        (remove_work if st == 'D' else write_work).append(p)
    say('round ' + str(rnd) + ': ' + str(behind) + ' commit(s), ' + str(len(rows)) + ' path(s) differ ('
        + str(sum(1 for r in rows.values() if r[4] == 'A')) + ' added, '
        + str(sum(1 for r in rows.values() if r[4] == 'D')) + ' deleted, '
        + str(sum(1 for r in rows.values() if r[4] not in 'AD')) + ' modified); to apply: index '
        + str(len(set_index)) + ', files ' + str(len(write_work)) + ', removals ' + str(len(remove_work))
        + '; conflicts ' + str(len(conflicts)))
    if conflicts:
        return 'conflict', H, O, conflicts
    targets = sorted(write_work + remove_work)
    tset, wset = set(targets), set(write_work)
    if dry:
        for p in targets:
            print(('  write  ' if p in wset else '  remove ') + p)
        for p, _ in set_index:
            if p not in tset:
                print('  index  ' + p)
        return 'dry', H, O, behind
    run_dir = os.path.join(gitdir, 'ffscoped-backup', time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + str(os.getpid()) + '-' + str(rnd))
    stage = os.path.join(gitdir, 'ffscoped-stage-' + str(os.getpid()))
    problems, done, kept = [], [], []
    try:
        if write_work:
            materialize(gitdir, [(p, o_ent_of[p]) for p in write_work], stage)
        for p in targets:
            full = os.path.join(ROOT, p)
            exp, loc = work[p], None
            if exp is not None:
                try:
                    loc = move_aside(full, run_dir, p)
                except FileNotFoundError:
                    problems.append('removed during the run, after the check: ' + p)
                    break
            try:
                if loc is not None:
                    cur = state_at(loc, p, filemode)
                    if not eq(cur, exp, filemode):
                        back = put_back(loc, full)
                        if not back or os.path.lexists(loc):
                            note_kept(run_dir, p, loc, exp)
                        problems.append('changed during the run, after the check: ' + p + ('; the changed file is back in its place, untouched'
                                        if back else '; a newer file is in its place, and the changed one is kept at ' + loc))
                        break
                    note_kept(run_dir, p, loc, exp)
                    kept.append((p, loc, exp))
                    hook = os.environ.get('FF_TEST_AFTER_ASIDE')
                    if hook and not ASIDE_HOOK_DONE:
                        ASIDE_HOOK_DONE.append(1)
                        subprocess.run(['bash', '-c', hook], cwd=ROOT, env=dict(ENV, FF_ASIDE=loc))
                if p in wset:
                    if not put_new(os.path.join(stage, p), full):
                        problems.append('a file appeared during the run: ' + p + ' (left as it is'
                                        + ('; main\'s version, moved aside before, is kept at ' + loc if loc else '') + ')')
                        break
                else:
                    d = os.path.dirname(p)
                    while d:
                        try:
                            os.rmdir(os.path.join(ROOT, d))
                        except OSError:
                            break
                        d = os.path.dirname(d)
            except BaseException:            # a failure or an interrupt mid-path: the moved file goes back before anything else
                if loc is not None and os.path.lexists(loc) and not os.path.lexists(full):
                    put_back(loc, full)
                raise
            done.append(p)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    dset = set(done)
    idx = [(p, e) for p, e in set_index if p not in tset or p in dset]
    if idx:
        lines = [('0 ' + ZERO if e is None else e[0] + ' ' + e[1]) + '\t' + p for p, e in idx]
        with_lock_retry(gitdir, 'update-index', '-z', '--index-info', inp=('\0'.join(lines) + '\0').encode(errors='surrogateescape'))
    fresh = [p for p in done if p in wset] + [p for p, e in idx if p not in tset and e is not None]
    if fresh:
        try:
            with_lock_retry(gitdir, 'add', '--refresh', '--pathspec-from-file=-', '--pathspec-file-nul',
                            inp=('\0'.join(fresh) + '\0').encode(errors='surrogateescape'), env=dict(ENV, GIT_LITERAL_PATHSPECS='1'))
        except GitError as e:
            say('NOTE: the stat information of the written files was not refreshed (' + str(e)[:160] + '); git status re-reads them')
    for p, loc, exp in kept:
        if not eq(state_at(loc, p, filemode), exp, filemode):
            problems.append('written after it was moved aside (a writer that had it open): ' + p + '; that write is kept at ' + loc)
    if problems:
        return 'raced', H, O, problems
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
        if not dry:
            prune_backups(gitdir, filemode)
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
                say('DRY RUN OK: nothing changed; a fast-forward of ' + str(info) + ' commit(s) would proceed; pause writes to the '
                    'paths listed above while it runs')
                return 0
            if res == 'conflict':
                for c in info[:50]:
                    print('  ' + c)
                if len(info) > 50:
                    print('  ... and ' + str(len(info) - 50) + ' more')
                say('REFUSED: ' + str(len(info)) + ' path(s) in the way' + ('' if rnd == 1 else ' (earlier rounds applied origin\'s files; nothing of a lane\'s was touched)'))
                return 5
            if res == 'raced':
                for c in info:
                    print('  ' + c)
                say('STOPPED: a write during the run; nothing of it was overwritten or deleted, and main was not moved. Settle the '
                    'path(s) with their writer, then run again (paths already done stay at origin\'s version)')
                return 11
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
