# Testing

```bash
bash tests/fleet_guard/run_tests.sh            # last line: RESULT: 89 passed, 0 failed
```

The suite runs on any machine with bash, Python and git: no scheduler, no GPU, no network.

## How it works

`run_tests.sh` copies the `slurm/` tools into a temporary folder with a test site file (`site.test.env`: a four-node fleet with a review threshold of 50 GPU-hours) and test caps (`caps.test.json`). It puts fake `squeue`, `sbatch`, `scontrol` and `sacct` (in `tests/fleet_guard/bin/`) first on `PATH`. The fakes keep a job table in a state file, record every `sbatch`, `scontrol` and `sacct` call, and can be told to fail (`squeue_fail`, `sacct_fail`, `sbatch_fail` marker files) or to answer slowly once (`sacct_slow`); `squeue -r` lists a collapsed pending array one task a line, `sbatch --array` writes the pending array as the one compressed row `squeue` shows, and every `squeue` call is logged with its task-list setting (`SLURM_BITSTR_LEN`). Nothing can reach a real scheduler, and no live queue, lock, log or caps file is touched.

## What it checks

| section | checks |
|---|---|
| 1 | a plain submission goes straight in, not held, `--no-requeue` |
| 2 | a full fleet: the job is accepted and held, queued with its node count, the message says do not resubmit |
| 3 | the duplicate guard, and `--allow-same-name` |
| 4 | a node frees: only the head of the queue is released; a second pass changes nothing |
| 5 | a newcomer does not jump the queue |
| 6 | a leaving job is not counted, its chained successor is |
| 7 | strict first in first out: a two-node job at the head is not overtaken |
| 8 | a job held for review is never released by the dispatcher |
| 9 | arrays need a throttle that fits, held or not |
| 10 | fail closed when the scheduler or accounting does not answer |
| 11 | an unknown lane tag is refused |
| 12 | the rate guard, and old submissions not counting |
| 13 | one combined dependency; an `afterok` chain that can never start is cancelled |
| 14 | the fleet lock: a fresh lock refuses, a stale one is taken over, the lock is gone afterwards |
| 15 | a failed `sbatch` leaves no queue entry and frees the lock |
| 16 | `wait_jobs.py` keeps waiting while `squeue` fails or accounting still says running; it polls every ten minutes by default, never faster than once a minute |
| 17 | the queue file is pruned, but not when the queue cannot be read |
| 18 | two copies of the tooling share one queue, lock and release log |
| 19 | the GPU-idle watchdog (`test_watchdog.sh`, 12 checks in fake mode) |
| 20 | the scoped fast-forward (`test_ffscoped.sh`, 31 checks on throwaway repositories) |
| 21 | the budget check reads the account only, in UTC windows of at most seven days, counts a job once, reads finished windows from its cache, and refuses with nothing cached when accounting is down |
| 22 | one locked transaction: two concurrent jobs under one cap (one is refused), the reservation, a job missing from the queue's snapshot, accounting taking over, expiry, a pending array counted task by task, the lock kept fresh through a slow budget read |
| 23 | two calculations for arrays: the budget charges every pending element (three pending 4 GPU-hour elements fill a 12 GPU-hour cap); the ceiling counts what an array may run at once (a pending `0-3%4` array is four nodes, a 100-task `%1` array one, a used-up throttle none), an element leaving, held then released then running, whole task lists, lists cut short (with and without a throttle shown), and the reservations of accepted jobs the queue does not show yet (counted while recent, not once the queue shows them ended, not when submitted held) |
| 24 | a valid caps file is required: none, empty, unreadable, not an object, and caps that are not a finite, non-negative number (NaN, Infinity, -1, a string, true, null) each refuse the submission; a lane without a cap beside others, a cap of 0, the deliberate `--over-cap-ok`, and the table without a caps file |
| 25 | the login-node load check (`test_login_check.sh`, 10 checks on the machine it runs on) |

`test_watchdog.sh` drives `pack_node.py` in fake mode with `PACK_FAKE_GPU_UTIL` set, so every fake GPU reports the utilisation the test chooses: idle long enough ends the job with the right receipt; active does not; a zero window disables it.

`test_login_check.sh` runs `tools/login_check.py` on whatever machine runs the suite: a verdict whose exit code matches it, as many cores as workers, WAIT with no cores when the memory asked for cannot fit or every core is excluded, and nothing written.

`test_ffscoped.sh` builds a bare origin, a shared clone and an outside clone that pushes edits, a deletion that empties a folder, a mode change, retargeted and new symlinks, a path with spaces and a non-ASCII name. It then checks a clean run, the dry run, a local edit, a staged change, an untracked file in the way, an unpushed lane commit, both forms of the race the tool exists for, a held lock, and that a lane's staged work elsewhere survives. Then the writers the tool must never overwrite: a file changed after the check and before the files are written (the review's reproduction, through a git wrapper that pauses there), the same for a file origin deletes and one it adds, a file recreated between its move aside and the new version, a write into a moved-aside file, a file written moments before the run, the dry run's list, and the backups kept and pruned.

## Adding a guard

Every new guard comes with a check in this suite before it is released. Extend the fakes when a guard needs a new Slurm behaviour. Keep the failure mode in view: a test that only checks the success path is not enough, so test what the guard does when Slurm answers wrongly or not at all.
