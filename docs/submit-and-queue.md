# Submitting and the fleet queue

`slurm/submit.sh` is the only way a fleet job reaches Slurm. It turns a spec into one `sbatch` call with the right account, partition, node shape and checkpoint warning, after a set of guards. `slurm/fleet_dispatch.sh` releases jobs the wrapper had to queue.

## Usage

```
slurm/submit.sh [--site NAME] [--devel] [--time LIMIT] [--name NAME] [--pack "extra pack_node args"]
                [--array 0-N%M --spec-dir DIR] [--nodes N] [--after ID[:ID..]] [--afterany ID[:ID..]]
                [--cpu-only [--cores N]] [--force-node --reason "..."] [--low] [--hold] [--over-cap-ok]
                [--comment "KEY=VALUE;KEY=VALUE"] [--allow-same-name] SPEC
```

| option | meaning |
|---|---|
| `--site NAME` | read `slurm/site.NAME.env` (default `local`) |
| `--devel` | the development partition and `DEVEL_MAX_TIME` |
| `--time` | the wall-time limit (default `MAX_TIME`) in any of Slurm's forms (`90`, `20:00`, `1:30:00`, `1-12`), read as Slurm reads it: rounded up to whole minutes; `0` and `UNLIMITED`, which mean no limit, are refused |
| `--name` | the job name (default: the spec's `job`); it must start with a lane tag that has a cap |
| `--pack` | extra arguments for `pack_node.py`, after the site's `PACK_EXTRA` |
| `--array 0-N%M --spec-dir DIR` | an array: task *i* runs `DIR/node_i.json`; the throttle `%M` is required |
| `--nodes N` | a multi-node job: the same spec on N whole nodes, one launcher per node ([multi-node.md](multi-node.md)) |
| `--after` / `--afterany` | chain behind earlier jobs (`afterok` / `afterany`); an `afterok` chain that can never start is cancelled; under a ceiling the job waits held in the fleet queue until its predecessors have ended and its nodes fit |
| `--cpu-only [--cores N]` | a spec without GPU tasks, on the site's `CPU_PARTITION` |
| `--force-node --reason` | a CPU-only spec holding a whole GPU node (charged as such), with a written reason |
| `--low` | filler priority (`sbatch --nice=1000`) |
| `--hold` | submit held; release with `scontrol release ID` |
| `--over-cap-ok` | skip the budget check (a deliberate, recorded exception) |
| `--comment` | passed to `sbatch --comment`: site job overrides such as JSC's `CUDA_MPS=1`; commas and quotes are refused |
| `--allow-same-name` | allow a second job under a name that is already pending or running (the next segment of a run) |

The job id goes to stdout; everything else goes to stderr. Give the wrapper a time limit you have priced ([operating-model.md](operating-model.md#pricing-a-job)).

## The guards

**The request is the wrapper's alone.** Before any guard, it drops the scheduler's own environment variables from everything it starts, and names them in a NOTE: `SBATCH_*`, the `SLURM_*` variables `sbatch` reads as options (those a job's environment carries, such as `SLURM_NTASKS`, which matter when a job submits its successor), and those `squeue`, `sacct` and `scontrol` read (`SQUEUE_*`, `SACCT_*`, `SCONTROL_*`, `SLURM_CLUSTERS`, `SLURM_TIME_FORMAT`). Left in, `SBATCH_ARRAY_INX` would make a one-node job an array of a hundred after its cost was reckoned, `SQUEUE_USERS` would hide the account's jobs from the ceiling, and `SLURM_TIME_FORMAT=relative` recent submissions from the rate guard. The fleet's state files (`FLEET_SUBMISSIONS_FILE`, `FLEET_RESERVATIONS_FILE`, `FLEET_QUEUE_FILE`) must take a line before anything is submitted (exit 4).

They run in this order. Every one fails closed: if the queue or the accounting cannot be read, nothing is submitted.

1. **Spec checks.** The spec must exist; a spec without GPU tasks needs `--cpu-only` (and a site with a CPU partition) or `--force-node --reason`; an array needs a positive throttle. The time limit is read as Slurm reads it (`slurm/timelimit.py`): rounded up to whole minutes, and refused (exit 2) when it is zero or unlimited, which Slurm reads as no limit, or not one of Slurm's forms.
2. **Review hold.** A worst case above `REVIEW_THRESHOLD_HOURS` is submitted held, with the message `HELD FOR REVIEW`. A reviewer checks it against the lane's plan and the principal releases it (`scontrol release ID`) or cancels it. The dispatcher never releases such a job.
3. **Fleet lock.** One submission at a time across the fleet: an atomic `mkdir` of `FLEET_LOCK_DIR` on the shared file system, always taken. Its holder refreshes it while it works; a lock nobody has refreshed for `FLEET_LOCK_STALE_S` seconds (180) belongs to a dead holder and is taken over. If the lock stays busy for `FLEET_LOCK_WAIT` seconds (45), exit 4.
4. **Queue first.** Before looking at this job, the dispatcher releases any queued jobs that now fit, so earlier jobs go first.
5. **Budget, inside the lock.** The worst case is the time limit as Slurm rounds it, times `GPUS_PER_NODE`, times the array tasks and nodes, rounded up to 0.001 GPU-hours (0 for `--cpu-only`); the same rounded limit is what `sbatch` is given and what the reservation records. `budget.py` refuses if the lane's used plus committed plus this job would exceed its cap, if the lane tag has no cap, or if accounting cannot be read. Exit 3. Because it runs inside the lock, after every earlier submission has been made, two submissions can never both spend the same headroom.
6. **One snapshot.** `squeue` is read once; if that fails, exit 4. All the following guards use that snapshot.
7. **Duplicate name.** A job of the same name already pending or running: exit 5, unless `--allow-same-name`. If the existing job is held, it is waiting for a node or for review, and must not be resubmitted.
8. **Rate.** `MAX_SUBMITS_PER_10MIN` or more submissions from the account in the last ten minutes: exit 6. They are counted from the queue's submit times and from the fleet's own record of every submission, a job both show once: the controller forgets an ended job `MinJobAge` seconds after it ends (Slurm's default is 300), sooner than the window. A loop is submitting, or it is a busy moment; either way, wait.
9. **Ceiling.** The nodes this job uses at once (nodes times the array throttle) may never exceed `MAX_NODES_IN_FLIGHT`, held or not (exit 4). If the account's nodes in flight plus this job exceed the ceiling, or others are already waiting, the job is **accepted and held** and appended to the fleet queue. The nodes in flight come from `slurm/inflight.py`, the helper the dispatcher uses too (see below). A chained job (`--after`, `--afterany`) is accepted and held and appended to the fleet queue whatever is in flight: Slurm starts a chained job the moment its predecessors end, without asking the wrapper, so a check at submission alone once let a two-node successor of one of four running jobs make five.

Then exactly one `sbatch` call, always with `--hold`, with `--no-requeue` (a node failure never restarts a job by itself) and one combined `--dependency` when chaining. Still inside the lock the job's records are written: its line in the submission record, its worst case in `FLEET_RESERVATIONS_FILE` as a reservation and, when it waits, its fleet queue entry. Only then is a job that may start now released (`scontrol release`): nothing starts unrecorded. A record that cannot be written leaves the job held (exit 7, the missing lines given); a release that fails leaves it held in the fleet queue, for the dispatcher. A reservation stays until accounting has reconciled the job, every task of it finished there. Until then the job counts as the larger of its worst case and what the queue and accounting already show for it: counted once and never dropped, whether it is only accepted, queued, running, or ended and gone from the queue before its record reaches accounting. An array is reconciled element by element. A reservation whose job neither the queue nor accounting shows expires a day after its time limit and a day after the queue last showed it, with a message. The reservation also records the nodes the job uses at once and whether it was submitted held or chained, so for a short while (`FLEET_RESERVATION_RECENT_S`, 120 seconds) a job that was accepted but is not in the queue's snapshot yet counts toward the ceiling too.

Every accepted submission also gets a line in the submission record, `FLEET_SUBMISSIONS_FILE`: time, job id, name, lane, array spec, tasks, nodes, time limit and dependency, written under the fleet lock and never pruned by the tooling. The rate guard reads it, and so does `wait_jobs.py`: an array's wait ends only when accounting shows every element of the recorded set, and an array the record does not have cannot be verified (exit 1, saying so) unless `--expect BASE:SPEC` gives its `--array`.

## Exit codes

| code | meaning |
|---|---|
| 0 | submitted, or accepted and queued (the message says `QUEUED BY THE FLEET GUARD` and gives the position) |
| 1 | usage error or a missing spec |
| 2 | refused spec or options (no GPU tasks, array without a throttle, bad `--comment`, a time limit that is zero, unlimited or not one of Slurm's forms) |
| 3 | refused by the budget check |
| 4 | the ceiling would be exceeded even alone, the fleet lock stayed busy, the queue could not be read, or a state file cannot take a line |
| 5 | a job of that name is already pending or running |
| 6 | the rate guard |
| 7 | accepted and held, its records not written: the job exists and will not start by itself; never submit it again. Write the lines the message gives, then release it (`scontrol release ID`), or cancel it |
| other | `sbatch` itself failed; nothing was queued and the lock was released |

None of these is a reason to retry automatically. Read the message, and take a refusal back to the human who approved the job.

## The fleet queue

When the ceiling is reached, the wrapper submits the job held and appends a line to `FLEET_QUEUE_FILE`:

```
2026-11-03T09:14:40Z|1234567|alpha-density-training|1
2026-11-03T09:20:02Z|1234571|alpha-density-eval|2|afterany:1234567
time UTC            |job id |name                  |nodes at once|what a chained job waits for
```

`slurm/fleet_dispatch.sh` releases queued jobs first in first out, as far as they fit under the ceiling. It runs:

- at the end of every fleet job, from `node.sbatch`, with `--departing JOBID` so the leaving job is not counted while its chained successors are;
- at the start of every submission;
- by hand, at any time: `bash slurm/fleet_dispatch.sh --site mysite`.

It is idempotent. It only ever releases jobs that are in the queue file and still pending with a user hold; it never submits, never cancels, and never touches a job held for review or held by a lane. If the job at the head does not fit, nothing behind it is released (strict first in first out, so a two-node job is not starved by one-node jobs). A line goes only on evidence, after a successful reading of the queue: the reading shows its job no longer held (released, running, cancelled or ended), or accounting shows the job ended. A job the reading does not show at all is kept, and said so: a held job never leaves the queue by itself, and one dropped on a single reading that missed it would stay held with nobody to release it. Each release is logged in `FLEET_DISPATCH_LOG`.

**Chained jobs.** The dispatcher releases a chained job only once Slurm shows no dependency for it that has not ended (the leaving job counts as ended), and then only if its nodes fit; until then the job is passed over and holds up nobody behind it. This works because Slurm keeps a held job's reason `JobHeldUser` whatever its dependency and updates the dependency list while the job is held. A pass right after the submission releases a chained job whose predecessors have ended already. Do not release a chained job by hand: once it is not held, Slurm starts it the moment its predecessors end, whatever is running then. If a predecessor dies without reaching its end hook (a node failure), its successor waits for the next pass: any submission, any fleet job's end, or the dispatcher by hand.

**Nodes in flight** (`slurm/inflight.py`, used by the wrapper and the dispatcher alike) are: running and configuring jobs, each array element on its own; pending jobs that are free to start (no dependency, not held); unheld jobs waiting on a dependency (a chained job released by hand, say), which start by themselves when their predecessors end: behind plain running jobs by what they need beyond the nodes those free (each predecessor counts as the larger of its own nodes and those of all the jobs chained behind it), otherwise in full; and accepted jobs the queue does not show yet, from their reservations, for `FLEET_RESERVATION_RECENT_S` seconds (120; keep it below Slurm's `MinJobAge`), unless they were submitted held or chained. Held and chained jobs are not counted until they can start.

**An array is two calculations.** `squeue` shows the pending part of an array as one line, such as `1234_[2-99%4]`. For the ceiling, that line counts the elements that may run at once: nodes times the smaller of the pending elements and the throttle less the array's elements already running. A pending `0-3%4` array is four nodes; a pending 100-task `%1` array is one. For the budget, every pending element is charged in full (`budget.py` reads the queue one task a line). Counting every element for the ceiling would hold the whole fleet back behind a long `%1` array; counting the line as one node let a fleet start more nodes than its ceiling. Both callers ask `squeue` for whole task lists (`SLURM_BITSTR_LEN=0`; otherwise it cuts them at 64 bytes). A list cut short counts its throttle in full; one with no throttle shown cannot be counted, so the submission is refused (exit 4) and the dispatcher releases nothing. The dispatcher reads every job state, so a job that has just ended is seen as ended.

**Several copies of the tooling.** If lanes pin their own copy, set `FLEET_QUEUE_FILE`, `FLEET_LOCK_DIR` and `FLEET_DISPATCH_LOG` to absolute paths on the shared file system in every copy's site file, so all copies queue and release from one list.

## What the batch script does

`slurm/node.sbatch` runs in the tooling folder. It evaluates `ENV_ACTIVATE`, writes a `START` line to `receipts/JOBS.log`, and starts `pack_node.py` in the background with `--receipt-dir receipts/<name>-<jobid>`. Slurm's checkpoint warning (`--signal=B:USR1@SIGNAL_BEFORE_END`) reaches only the batch shell, so the shell traps `USR1` and `TERM` and forwards them to the launcher, then keeps waiting until the launcher has really exited. Without that trap, bash dies of the signal and Slurm tears the job down before any task can checkpoint. At the end it writes `DONE`, `FAILED` or `REFUSED` to the job log and runs the dispatcher (best effort, 90 seconds at most).

Slurm's own output goes to `logs/<name>-<jobid>.out` and `.err`; the launcher's watchdog line, if any, is there too.

## Multi-node jobs

`--nodes N` runs the same spec on N whole nodes under `srun`, one launcher per node, receipts in `receipts/<name>-<jobid>/node-<k>/`. The tasks see `SLURM_NNODES`, `SLURM_NODEID` and `SLURM_JOB_NODELIST`. See [multi-node.md](multi-node.md) for the qualified NCCL recipe.
