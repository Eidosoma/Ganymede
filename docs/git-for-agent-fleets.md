# Git for agent fleets

The original fleet ran fifteen agent sessions against **one** git checkout on a parallel file system: one working tree, one index, one `index.lock`, everyone on `main`, each agent writing only its own folder. That setup is simple and keeps every lane's work in one place, but plain git commands do not survive it. This page explains why and what to use instead.

## What goes wrong with plain git

- **The index lock is shared.** Every command that writes the index takes `.git/index.lock`. While one agent holds it, every other agent's commit waits or fails.
- **Whole-tree commands are slow on a parallel file system.** A cold `stat` cost about 2.3 ms on GPFS. A plain partial commit (`git commit -- path`) refreshes the whole index, twice, once under the lock: on a 248,000-file tree that took minutes, and every other agent waited.
- **`git status` over the whole tree** holds the lock for its whole scan unless optional locks are off; with `--ignored` it took 20 minutes.
- **`git merge` and `git pull`** refuse while anyone has staged entries, and a fast-forward rewrites the working tree under the lock.
- **A killed git process can leave the lock behind.** A commit killed mid-way left staged files that blocked everyone's merges; a step cut off by a crashed login node left an empty lock that stood for 13 hours until a human removed it.

## lanegit.sh: commits and pushes for many agents

```
git/lanegit.sh status <lane-folder>                               scoped status; takes no lock
git/lanegit.sh commit <lane-folder> -m "<message>" -- <paths...>  explicit paths under the lane folder only
git/lanegit.sh push   <lane-name> [<commit>]                      merge-tree route; verifies the commit is on origin
git/lanegit.sh check                                              lock holder, staged entries by lane, unpushed count
git/lanegit.sh index  <lane-folder> -- <paths...>                 bring the shared index up to date for paths
```

**Commit.** The paths must lie inside the lane's folder; a folder expands to its modified, new and deleted files, and to whatever is staged against HEAD; a path gone from the disk expands to every file HEAD and the index hold at or under it, so naming a deleted folder commits its removal, and a path that never existed is refused. The commit is built in a *private* index: `read-tree HEAD`, `update-index` for the named paths, `write-tree`, `commit-tree`. HEAD then moves with `update-ref`, guarded by the HEAD it was built on; if another lane moved HEAD meanwhile, it rebuilds and tries again. Only the named paths are ever read. Afterwards the shared index is set to the committed versions of those paths, briefly, under the lock; it is never re-read from the working files, so an edit made since the commit stays visible as a change. If the lock stays busy for `LANEGIT_INDEX_WAIT` seconds (120), the paths are recorded as pending and replayed by the lane's next `status`, `commit` or `index` call; the commit itself already stands.

It prints `COMMITTED <hash>`, then `INDEX UPDATED` or `INDEX PENDING`, or `NOTHING TO COMMIT` when the paths equal HEAD. It refuses new files over `FLEET_MAX_NEW_FILE_KB` (256 KB), any file over `FLEET_MAX_FILE_MB` (10 MB), and ignored paths. `FLEET_ALLOW_LARGE=1` lifts the size rules for one deliberate exception, which belongs in the commit message.

**Push.** Fetch, then a merge commit of origin's main and the local main built with `merge-tree --write-tree` and `commit-tree`, which touch neither the index nor the working tree; push it; fetch again and verify the commit is on origin; up to four tries. Local main is never fast-forwarded here, so it trails origin by the lanes' merge commits and by anything pushed from outside. That is harmless, and `ffscoped.py` levels it when an operator decides to.

**Check.** Whether a lock is held (with its age, and the git processes on this login node only: a holder on another node shows no process here, so a lock is never judged stale from one node), the staged entries by lane (set `LANEGIT_LANES_PREFIX` if lane folders live under a common folder), and the local commits not yet on origin.

**Credentials.** By default the tool uses your own git credential setup. `FLEET_GIT_TOKEN_FILE` names a file holding a token for HTTPS; it is read only by git's credential helper, and the tool's output is filtered so the token never appears.

## ffscoped.py: bringing outside work in

When someone pushes to origin from outside (a laptop, another site), the shared checkout needs that work. `git merge --ff-only` is the wrong tool here: before merging, git checks every tracked file. On a 320,000-file checkout that took 15 minutes, an agent committed in that window, and git refused its final ref update after the files and index were already written, leaving the checkout split between two states.

```bash
python3 git/ffscoped.py --dry-run     # what would change, and whether anything is in the way
python3 git/ffscoped.py               # do it
```

It reads only the paths that differ between local main and origin/main. Each must hold either main's or origin's version, in the working file and in the index; anything else is a lane's edit, and the tool refuses and lists it before writing anything. A file to be replaced or removed must also have been quiet for a minute (`FF_SETTLE_S`). Then it writes origin's versions into a staging folder in `.git` and goes path by path: the working file is moved into the run's backup folder (`.git/ffscoped-backup/`) and checked again, and origin's version is put in place through a hard link, which fails rather than replace a file that appeared meanwhile. Deleted files are moved aside the same way, and emptied folders removed. Then it sets those paths' index entries to origin's and moves main with `update-ref` guarded by its old value. If a lane commits in that moment the move is refused, and the next round finishes after the lane's push; every round is idempotent. Every other index entry, a lane's staged work included, is left alone. On the same checkout it brought in 93 changed paths in 1.5 seconds.

**Pause the writers while it runs.** The dry run lists every path the tool would write, remove or set in the index; the agents that write those paths pause until it is done. The tool itself never overwrites or deletes a write: a change made after its check is put back, or kept in the backup folder, and the round stops before main moves (exit 11), naming the path. One case only the pause covers: a writer that opened a file before the update and writes into it afterwards writes into the moved-aside copy, which is kept and, if the write comes before the end of the run, named. Backups are kept for a week (`FF_KEEP_DAYS`); an older one whose files still match what was moved aside is removed at the next run, any other is kept and named. Doing without the pause would take a write lock that every writer respects.

Exit codes: 0 done or nothing to do; 2 not a checkout or HEAD not on `main`; 3 local main has a commit origin lacks (a lane's unpushed commit; it waits two minutes for the push first); 4 the index lock is held; 5 something is in the way, or a file to be touched was written in the last minute (nothing changed); 6 an unsupported entry; 8 a git failure; 10 main kept moving; 11 a write during the run (the round stopped before main moved; the write kept).

## Size hooks

`git-hooks/pre-commit` refuses a commit that adds or changes a file over `FLEET_MAX_FILE_MB` (10), with rename detection off, so a file renamed and grown is measured under its new name; `git-hooks/pre-push` refuses a push whose new commits carry such a blob, whichever machine made them. Install once per clone with `bash git-hooks/install.sh` (it sets `core.hooksPath`). `lanegit.sh` applies the same rule itself, because `commit-tree` runs no hooks. Outputs belong on scratch or in project space outside git; receipts, protocols, scripts and tests go in.

## House rules

- Lanes use `lanegit.sh` and nothing else for git. No `git add -A`, no whole-tree `git status`, no `git merge`, no `git pull`, no `git gc`.
- Never kill a git process that holds the lock, and never remove a lock from one login node without checking the others. A lock is a human's call.
- Keep the object store in shape. Loose objects pile up with many small commits: 24,000 of them made commits slow. An incremental `git repack -d -l` packs them in minutes without any lock (3 minutes 24 seconds for 24,000); a full `git gc` rewrites every pack and took two hours on the same repository.
- Commit only files you have read, and keep new files under the size limit.
