# Testing

```bash
bash tests/fleet_guard/run_tests.sh            # last line: RESULT: 148 passed, 0 failed; exit status 0, and 1 when any check fails
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
| 12 | the rate guard, and old submissions not counting; submissions the queue no longer shows still counted from the submission record, a job in both counted once, and the record's lines for a plain and a chained array submission |
| 13 | one combined dependency; an `afterok` chain that can never start is cancelled |
| 14 | the fleet lock: a fresh lock refuses, a stale one is taken over, the lock is gone afterwards |
| 15 | a failed `sbatch` leaves no queue entry and frees the lock |
| 16 | `wait_jobs.py` keeps waiting while `squeue` fails or accounting still says running; it polls every ten minutes by default, never faster than once a minute; an array is followed element by element (each element's receipt under its own job id), one element can be waited for alone, and a two-node job needs both nodes' receipts; a failed accounting read is never an answer, and the report comes from the read that ended the wait; an array waits until accounting shows every element of the set in the submission record (one shown of two keeps it waiting), an element that never started is accounted for, and an array without a record cannot be verified unless `--expect` gives it |
| 17 | the queue file is pruned, but not when the queue cannot be read |
| 18 | two copies of the tooling share one queue, lock and release log |
| 19 | the GPU-idle watchdog (`test_watchdog.sh`, 17 checks: fake mode, and readings that differ by slot through a stand-in GPM sampler and a fake `nvidia-smi`) |
| 20 | the scoped fast-forward (`test_ffscoped.sh`, 31 checks on throwaway repositories) |
| 21 | the budget check reads the account only, in UTC windows of at most seven days, counts a job once, reads finished windows from its cache, and refuses with nothing cached when accounting is down |
| 22 | one locked transaction: two concurrent jobs under one cap (one is refused), the reservation, a job missing from the queue's snapshot, accounting taking over, expiry, a pending array counted task by task, the lock kept fresh through a slow budget read |
| 23 | two calculations for arrays: the budget charges every pending element (three pending 4 GPU-hour elements fill a 12 GPU-hour cap); the ceiling counts what an array may run at once (a pending `0-3%4` array is four nodes, a 100-task `%1` array one, a used-up throttle none), an element leaving, held then released then running, whole task lists, lists cut short (with and without a throttle shown), and the reservations of accepted jobs the queue does not show yet (counted while recent, not once the queue shows them ended, not when submitted held) |
| 24 | a valid caps file is required: none, empty, unreadable, not an object, and caps that are not a finite, non-negative number (NaN, Infinity, -1, a string, true, null) each refuse the submission; a lane without a cap beside others, a cap of 0, the deliberate `--over-cap-ok`, and the table without a caps file; a job name with an empty lane tag (`-oops`) refused, by the wrapper and by the budget check |
| 25 | the launcher's task shell under the checkpoint signal: a compound command waits for the save; a program without a handler dies of the signal and nothing after it runs; a program started with `&` and waited for has its save counted, one without a handler still fails, and a background save beside a foreground program that dies masks nothing; a missing task folder refused before anything starts, and a start that fails after another; a program that saves at once, ten runs each for exit 0 and exit 3, and twenty runs of the bare task shell; a CPU pool under the signal: the worker waits for its command's save and the rest is kept in the resume file, a command without a handler is kept, a failure before the signal is still a failure (`test_launcher.sh`, 20 checks) |
| 26 | lanegit's shared-index update: a deferred update replayed later records the committed version, never a newer edit; folder commits take staged changes and staged deletions (`test_lanegit.sh`, 7 checks on throwaway repositories) |
| 27 | a reservation stays until accounting has the job: accepted, queued, ended and gone from the queue before accounting has it (still counted), reconciled (retired); a running job counted once; an array reconciled element by element; a job long in the queue still counted; expiry a day after the queue last showed the job |
| 28 | the time limit read as Slurm reads it (`slurm/timelimit.py`): Slurm's six forms, rounded up to whole minutes (16 cases); zero, no limit and anything that is not one of the forms refused (21 cases); GPU-hours rounded up; 1,000 one-second tasks are 1,000 one-minute tasks, 66.667 GPU-hours, held for review; `00:20:01` is 21 minutes and `20` twenty minutes; `1-00` is a day; `--time 00:00:00`, `0`, `UNLIMITED` and `-1` refused; a lane with a queued job that has no limit admits nothing more |
| 29 | chained jobs: submitted held and queued with what they wait for, whatever is in flight; a two-node successor of one of four running jobs is released only when two nodes are free, and of two successors of one job only one takes its node; the moments while a predecessor ends; a predecessor already ended (released at once); a chained job that waits holds up nobody behind it; a dependency Slurm has cleared, or still lists; a failed `afterok` (never released); unheld chained jobs counted by what they need beyond their predecessors' nodes; a chained job its lane holds stays out of the queue |
| 30 | the login-node load check (`test_login_check.sh`, 12 checks on the machine it runs on, the login GPU's rule with a fake `nvidia-smi`) |

`test_watchdog.sh` drives `pack_node.py` in fake mode with `PACK_FAKE_GPU_UTIL` set, so every fake GPU reports the utilisation the test chooses: idle long enough ends the job with the right receipt; active does not; a zero window disables it. Then readings that differ by slot: one slot idle and one never read (not idle: the review's case), a slot that GPM misses read by `nvidia-smi` instead (idle, then busy), a NaN reading, and one value per fake slot.

`test_login_check.sh` runs `tools/login_check.py` on whatever machine runs the suite: a verdict whose exit code matches it, as many cores as workers, WAIT with no cores when the memory asked for cannot fit or every core is excluded, and nothing written. A fake `nvidia-smi` then shows the login GPU's rule counting what you hold already: 10 GiB held and 10 more is over 16, 4 more is not. The other process on the fake card has a PID that cannot exist, so it counts as someone else's whoever runs the suite, root included.

`test_ffscoped.sh` builds a bare origin, a shared clone and an outside clone that pushes edits, a deletion that empties a folder, a mode change, retargeted and new symlinks, a path with spaces and a non-ASCII name. It then checks a clean run, the dry run, a local edit, a staged change, an untracked file in the way, an unpushed lane commit, both forms of the race the tool exists for, a held lock, and that a lane's staged work elsewhere survives. Then the writers the tool must never overwrite: a file changed after the check and before the files are written (the review's reproduction, through a git wrapper that pauses there), the same for a file origin deletes and one it adds, a file recreated between its move aside and the new version, a write into a moved-aside file, a file written moments before the run, the dry run's list, and the backups kept and pruned.

`test_launcher.sh` runs the launcher in fake mode and sends it `SIGUSR1` while a checkpoint-aware program saves for two seconds. As a single command and as a compound one (`program; echo post`), the launcher waits for the save and the receipt reads a checkpoint segment; a program that does not handle the signal dies of it (exit 138) and nothing after it runs. A program started with `&` and waited for (`program & wait $!`) has its save counted; without a handler, that form still fails; and a background save beside a foreground program that dies of the signal does not mask the failure. Each program announces itself only once it runs, so the signal never lands between commands. A task whose folder is missing is refused before anything starts; a start that fails after another task has started stops that task and leaves a receipt with `launch_error`.

`test_lanegit.sh` commits through `lanegit.sh` on a throwaway repository with the index lock held, edits the file again, and checks that the replayed index update records the committed version, so the newer edit stays a change that the next folder commit takes; then a staged change and a staged deletion, each taken by a folder commit.

**CI.** [`.github/workflows/tests.yml`](../.github/workflows/tests.yml) runs the suite on every push and pull request, and the run fails when any check fails. `probes/test_nccl_stop.py` (PyTorch on CPU, two processes) checks the NCCL probe's stopping rule and its parameter comparison; it is not part of the suite.

## Adding a guard

Every new guard comes with a check in this suite before it is released. Extend the fakes when a guard needs a new Slurm behaviour. Keep the failure mode in view: a test that only checks the success path is not enough, so test what the guard does when Slurm answers wrongly or not at all.
