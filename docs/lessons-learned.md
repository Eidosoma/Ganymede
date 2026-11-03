# Lessons learned

Ganymede's first version drew on a year of running agent fleets, with safeguards designed around many of the risks we already anticipated. Six and a half weeks of running a dozen agents on one JUPITER allocation then brought further lessons: incidents and near misses that led us to refine existing safeguards and add new ones. This page lists those incidents and the rules they introduced, strengthened or clarified. The numbers are from the original deployment on JUPITER Booster.

## Jobs and the scheduler

**The checkpoint warning reached nobody.** Slurm's timed warning (`--signal=B:USR1@N`) goes to the batch shell only. Without a trap, bash died of the signal, the job showed `ExitCode 0:10`, and Slurm tore it down before any task could checkpoint. *Rule:* the batch script traps `USR1` and `TERM`, forwards them to the launcher, which forwards them to every task, and keeps waiting until the launcher has really exited.

**A relative path killed a job at its start.** The batch script runs in the tooling folder, so a spec path relative to the agent's folder pointed nowhere. *Rule:* the wrapper makes spec paths absolute before calling `sbatch`.

**A two-node job was charged twice.** Accounting's `AllocTRES` carries a typed and an untyped GPU entry; a format guessed from the string counted a multi-node job's GPUs once more per node (1.58 GPU-hours for 0.79). *Rule:* the caller names the format, and both formats are tested on real strings.

**Two approved jobs could share one budget.** Found in a review of the public release, and reproduced: the budget check ran before the fleet lock, so two submissions read the same headroom and both passed (two 4 GPU-hour jobs under a 4 GPU-hour cap). It took no rule-breaking, only two agents with two separately approved jobs. *Rules:* the budget check and the submit are one locked transaction; an accepted job's worst case is reserved until the queue or accounting shows it, because serialising alone does not cover a job missing from the next snapshot; the lock's holder keeps it fresh, so a slow check cannot lose it.

**One line in the queue was four jobs.** Found in the same review, and reproduced: `squeue` shows the pending part of an array as one line, so the ceiling counted a pending `0-3%4` array as one node and let the next job start on a fifth node under a four-node ceiling. The budget had the same blind spot, closed by the first fix: it now reads the queue one task a line. *Rules:* two calculations. The budget charges every pending element in full; the ceiling counts what an array may run at once, its throttle less its elements already running, so a long `%1` array holds one node's place, not a hundred. Ask `squeue` for whole task lists (`SLURM_BITSTR_LEN=0`, or it cuts them at 64 bytes), and count a list cut short at its throttle, or refuse.

**A missing file meant no limit.** Found in the same review, and reproduced: the budget check read a missing caps file as an empty list of caps and refused an unknown lane only when the list was not empty, so a first installation, or a copy of the tooling without its caps, let every lane run uncapped (a job named `mystery-uncapped` went in); a cap of NaN could never be exceeded. *Rules:* a guard's configuration is part of the guard. No valid caps file, no submission; a cap is a finite, non-negative number; the deliberate override stays explicit (`--over-cap-ok`).

**The shell died of the checkpoint signal.** Found in the same review, and reproduced: a task written as two commands left its shell as the program's parent; the forwarded signal killed the shell at once, the launcher took that as the task's end, and the job could finish while the program was still saving. *Rule:* the task shell traps the signal, waits for the running command, exits with its status and starts nothing more.

**A failing suite looked green.** Found in the same review, and reproduced: the test runner printed its failures and exited 0, so a script or CI that reads only the exit status saw success. *Rule:* a suite ends on its failure count; the exit status is part of the result.

**Seen is not settled.** Found in a second review, and reproduced: a reservation was dropped as soon as the queue showed its job, so a job that finished and left the queue before accounting had its record counted nothing, and its hours could be spent again. *Rule:* a reservation lives until accounting has every task of its job; until then the job counts as the larger of its worst case and what the queue and accounting show.

**A failed read is not an answer.** Found in the same review, and reproduced: the waiter read accounting a second time to report, that read could fail and come back partial, and a failed job missing from it turned the result into success. *Rule:* a read whose exit status is not 0 is no picture of anything; report from the read you trusted.

**A second was priced as a second.** Found in a third review, and reproduced: the wrapper read the hours from `--time` as written, but Slurm rounds a limit up to whole minutes, reads `20:00` as twenty minutes and `0` as no limit at all. A thousand one-second tasks were priced 1.111 GPU-hours for what Slurm runs as 66.667, and `--time 0` passed a zero cap. *Rule:* read an input the way the system that acts on it reads it, with one parser for everything that depends on it, and refuse what has no bound.

**A dependency is a start nobody checks.** Found in the same review, and reproduced: the ceiling was checked when a job was submitted, but a chained job starts when its predecessors end, without asking anyone, so a two-node successor of one of four running jobs made five. *Rule:* a job that will start on a later event is held until that event, and admitted then by the same check as every other job.

**Alive is not the same as collected.** Found in the third review's second part, and reproduced: the task shell waited again for a program only while the program was alive, so one that saved and exited at once left the interrupted wait's 138, and a clean checkpoint read as a failed job. Waiting again regardless was no answer either: bash can lose a status when a signal interrupts its wait at the moment the program ends, and the second wait then blocks for good. *Rule:* a supervisor that must outlive signals does not lean on a blocking wait the signal can interrupt; it watches for the end, then collects.

**The middle layer died first.** Found in the same review, and reproduced: the checkpoint signal reached a pool worker that had no handler, so it died at once while its command was still saving, and the launcher reported the pool before the save had finished. *Rule:* every process between the signal and the work outlives the signal and waits for the work; work not done is written down for the next segment.

**A partial answer looked complete.** Found in the same review, and reproduced: the waiter took the array elements accounting showed as the whole array. *Rule:* know the expected set from your own record, and wait until the answer covers it.

**The scheduler forgets sooner than the guard looks back.** Found in the same review, and reproduced: the rate guard counted the submissions the controller still remembered, and Slurm forgets an ended job after five minutes by default, half the guard's window. *Rule:* keep your own record of what a guard counts, for at least the guard's window.

**Silence read as zero.** A failed `squeue` looked like an empty queue, so a waiter returned while the job was still running, and a budget check could read "nothing used". *Rule:* every tool fails closed. A wrapper that cannot read the queue submits nothing; a budget check that cannot read accounting refuses; a waiter believes an empty queue only when accounting shows the job has ended.

**A full fleet made agents retry.** The first ceiling refused jobs when the fleet was full, and the agent had to come back later. Agents that come back later are agents that loop. *Rule:* accept and hold. The job is queued and released first in first out by a dispatcher that runs at every job's end and every submission.

**Runaway submissions are one bug away.** Nothing in the first wrapper stopped an agent's retry loop from flooding the queue. *Rule:* refuse a duplicate name, refuse a burst (12 in ten minutes), require a throttle on arrays, submit `--no-requeue`, allow one submission at a time fleet-wide, and keep loops and retries out of the tools entirely.

**An environment variable was silently overwritten.** A job meant to run under the site's MPS ran without it, because the wrapper set the `--comment` value itself and replaced the agent's environment variable. *Rule:* site overrides go through the `--comment` argument, never the environment, and the job log records them.

**The controller went down.** During a Slurm outage at the site, jobs whose tasks had finished were still charged until Slurm recorded their end: four jobs accrued 19.52 GPU-hours of idle charges. The receipts, written by the launcher with real end times, were the evidence for a refund request. *Rules:* during an outage, submit nothing, cancel nothing, and treat a receipt with `ended` and `all_ok` as the record of what finished; after recovery, check accounting by job name before any resubmission, because a job accepted in the controller's last seconds can reappear.

**Accounting cut off large answers.** `sacct` responses of about 1.8 MB were cut off intermittently, with exit code 1 and empty output. *Rules:* read your account only, in windows, and keep finished weeks in a cache, so a check reads only the current week (the whole-period read had cost 5 to 8 seconds of database work on every submit); assert the exit code; let the budget check refuse rather than guess.

**Two clocks.** The login nodes ran on local time; Slurm prints local time unless told otherwise; the site's quota tool trailed accounting by about a day. *Rules:* `TZ=UTC` on every Slurm query, `date -u` for every record, and compare the site's figures with accounting only after a quiet day.

## GPUs and nodes

**GPUs sat idle behind stalled runners.** One job held a node for about five hours after its runner stalled, and about 19 of its 24 GPU-hours were idle. Another lost about 20 of 37 to a trainer that hung at exit. *Rule:* the GPU-idle watchdog. When every GPU is under 1 % for 45 minutes, the job ends and is recorded as a failure to read. Every job gets it, and a spec cannot opt out.

**Missing is not idle.** Found in the same review, and reproduced: the watchdog judged a job on the cards it could read, so one card idle and one never read ended the job as if both were idle. *Rule:* a reading for every card, from a second source when the first misses one; a sample with any card unread counts as active.

**GPU slot 0 was slower.** In job after job, the task on GPU 0 ran 15 to 30 % slower, because its feeder held core 0, where the operating system, interrupts, the Slurm step and the launcher's own samplers live. *Rules:* reserve the lowest cores, give feeders the highest cores of their processor, and give each feeder cores local to its GPU.

**Idle GPUs at the end, too.** The packing rule stops a job from starting with an idle GPU, but CPU sidecars that outlast the GPU work leave the cards idle until they finish. *Rule:* size sidecars to end with the GPU work, and put long CPU work beside the next GPU job.

**Two numbers for "busy".** `nvidia-smi` utilisation counts the time any kernel ran; small models showed 30 to 45 % utilisation with 1 to 15 % of the SMs active. *Rule:* every receipt reports both, from the Hopper GPM counters, calibrated against known duty cycles.

## Files and storage

**The project file system ran out of files, not bytes.** The project space has a quota on the number of files. When it was reached, file creation failed, git could not create its lock file, and a job started and wrote nothing at all. *Rule:* code, protocols, receipts and one archive per job live in the project space; anything with more than about a hundred files per job lives on scratch.

**Home filled up with tool state.** Agent transcripts and caches filled a 21 GB home quota. *Rule:* caches and outputs go to project or scratch space (`HF_HOME`, `TORCH_HOME`, `PIP_CACHE_DIR`), and agent tool state is watched.

## Login nodes

**Your sessions share one limit.** All of a user's processes on a login node share one memory limit (143 GiB per user on JUPITER at the time). One night the fleet's sessions reached 138 GiB and processes were killed. *Rules:* check the slice before anything large, keep your own footprint under about 20 GiB, and cap what you start.

**The first cores were never free.** Tests pinned to the first two cores of their allowance timed out at the five-minute limit: other users kept core 0 86 to 100 % busy, and the tests ran twice as slow. *Rule:* measure the cores at launch and pin to the least busy, never 0 and 1 (`tools/login_check.py`).

**Suites were sized by hand.** A two-thread test suite took about three hours and six workers about half an hour; under a node load of 230 to 250, test files that passed on a quiet node hit the five-minute cap. *Rules:* size the workers to the load (12 when the 5-minute load is under 30, else 6, none when fewer than six cores are idle), and split a test file that hits the cap under load.

**The login GPU filled up mid-run.** On one evening four probes ended up over the 16 GiB limit while other users were on the card; three had started on a free card. A watchdog ended only the probe's own process group and missed workers in their own sessions. *Rules:* at or under 16 GiB for the whole run while anyone else is on the card, and a watchdog that ends every process of the item.

**The node fell over for others' reasons.** Twice a login node ran out of memory because of other users' processes. The parallel file system faltered, and every agent session whose program files lived on it died, while bash and tmux on the local disk survived. *Rule:* after a crash, read `dmesg` and your slice's `memory.events` before touching anything, then re-arm one watcher per job and resubmit nothing.

**Stopping a suite left it running.** Killing a test runner's process group left its units running, because `timeout` starts its own group; another time `pgrep -f` on the runner's name matched the shell doing the killing. *Rule:* stop by explicit PID, workers and children included, and check that nothing is left.

## Git

**A partial commit held the lock for minutes.** On a 248,000-file tree on GPFS, `git commit -- path` refreshed the whole index twice, once under the lock, at about 2.3 ms a `stat`. *Rule:* commits are built in a private index from HEAD plus the named paths, and the shared index is touched only briefly afterwards (`lanegit.sh`).

**A killed commit blocked everyone.** A commit killed mid-way left staged files behind, and every lane's merge refused. *Rule:* never kill a git process that holds the lock.

**A crash left a lock for 13 hours.** A login node went down mid-step and left an empty `index.lock`. Commits still worked through the private index, but every lane's index update was deferred, and those that stayed idle kept stale entries that would have blocked a merge. *Rules:* a human removes a lock after checking every node; then the deferred updates are replayed for idle lanes too.

**A merge was raced.** `git merge --ff-only` spent 15 minutes checking 321,543 tracked files before merging; an agent committed meanwhile, and git refused its final ref update after the files and index were written. *Rule:* bring outside work in with `ffscoped.py`, which reads only the changed paths and moves main with a guarded ref update (93 paths in 1.5 seconds).

**The merge tool could overwrite a write.** Found in the same review, and reproduced: the scoped fast-forward checked each file, then wrote over it later; an agent's edit in between was overwritten, the tool reported success, and git showed a clean checkout, so the edit, never committed, was simply gone. *Rules:* move the old file aside and check it again before anything replaces it; put the new version in with an operation that fails rather than replace (a hard link); stop and keep, never resolve, when a writer shows up; and pause the writers of the paths being updated, because only that covers a file held open across the update.

**A replayed index update staged a newer edit.** Found in the same review, and reproduced: a deferred update of the shared index, replayed later, hashed the working file, so an edit made after the commit was staged, and the next folder commit, which looked only at unstaged changes, said nothing had changed. *Rules:* the shared index takes the committed versions; a folder commit looks at what is staged too.

**Loose objects slowed every commit.** Many small commits left 24,000 loose objects. *Rule:* repack incrementally when they pass 20,000 (`git repack -d -l`: 3 minutes 24 seconds, no lock); a full `git gc` took two hours.

## People and agents

**A word is not a habit.** An approval given for one job was never meant for the next one, and a raised cap was never meant as an approval. *Rule:* one word per job, recorded verbatim with the time it was first seen, spent by the submit ([operating-model.md](operating-model.md)).

**Scaled-down qualifications underprice.** A bounded pilot priced the full run's histories 50 % low and its memory 21 % low. *Rule:* price from a run at the registered size, with a margin (1.2 times the measured wall time plus a quarter of an hour).

**A short smoke hid a counting bug.** A 32-chunk smoke test passed where the full length would have shown a 256-against-255 mismatch. *Rule:* one pass at the full registered length before a job is priced.

**Numbers from memory drift.** *Rule:* a number in a decision is computed from the files at the time of writing, and checked before it is written.
