# Lessons learned

Every guard in Ganymede exists because something went wrong, or nearly did, in six and a half weeks of running a dozen agents on one allocation. This page lists the incidents and the rule each one produced. The numbers are from the original deployment on JUPITER Booster.

## Jobs and the scheduler

**The checkpoint warning reached nobody.** Slurm's timed warning (`--signal=B:USR1@N`) goes to the batch shell only. Without a trap, bash died of the signal, the job showed `ExitCode 0:10`, and Slurm tore it down before any task could checkpoint. *Rule:* the batch script traps `USR1` and `TERM`, forwards them to the launcher, which forwards them to every task, and keeps waiting until the launcher has really exited.

**A relative path killed a job at its start.** The batch script runs in the tooling folder, so a spec path relative to the agent's folder pointed nowhere. *Rule:* the wrapper makes spec paths absolute before calling `sbatch`.

**A two-node job was charged twice.** Accounting's `AllocTRES` carries a typed and an untyped GPU entry; a format guessed from the string counted a multi-node job's GPUs once more per node (1.58 GPU-hours for 0.79). *Rule:* the caller names the format, and both formats are tested on real strings.

**Silence read as zero.** A failed `squeue` looked like an empty queue, so a waiter returned while the job was still running, and a budget check could read "nothing used". *Rule:* every tool fails closed. A wrapper that cannot read the queue submits nothing; a budget check that cannot read accounting refuses; a waiter believes an empty queue only when accounting shows the job has ended.

**A full fleet made agents retry.** The first ceiling refused jobs when the fleet was full, and the agent had to come back later. Agents that come back later are agents that loop. *Rule:* accept and hold. The job is queued and released first in first out by a dispatcher that runs at every job's end and every submission.

**Runaway submissions are one bug away.** Nothing in the first wrapper stopped an agent's retry loop from flooding the queue. *Rule:* refuse a duplicate name, refuse a burst (12 in ten minutes), require a throttle on arrays, submit `--no-requeue`, allow one submission at a time fleet-wide, and keep loops and retries out of the tools entirely.

**An environment variable was silently overwritten.** A job meant to run under the site's MPS ran without it, because the wrapper set the `--comment` value itself and replaced the agent's environment variable. *Rule:* site overrides go through the `--comment` argument, never the environment, and the job log records them.

**The controller went down.** During a Slurm outage at the site, jobs whose tasks had finished were still charged until Slurm recorded their end: four jobs accrued 19.52 GPU-hours of idle charges. The receipts, written by the launcher with real end times, were the evidence for a refund request. *Rules:* during an outage, submit nothing, cancel nothing, and treat a receipt with `ended` and `all_ok` as the record of what finished; after recovery, check accounting by job name before any resubmission, because a job accepted in the controller's last seconds can reappear.

**Accounting cut off large answers.** `sacct` responses of about 1.8 MB were cut off intermittently, with exit code 1 and empty output. *Rules:* read your account only, in windows, and keep finished weeks in a cache, so a check reads only the current week (the whole-period read had cost 5 to 8 seconds of database work on every submit); assert the exit code; let the budget check refuse rather than guess.

**Two clocks.** The login nodes ran on local time; Slurm prints local time unless told otherwise; the site's quota tool trailed accounting by about a day. *Rules:* `TZ=UTC` on every Slurm query, `date -u` for every record, and compare the site's figures with accounting only after a quiet day.

## GPUs and nodes

**GPUs sat idle behind stalled runners.** One job held a node for about five hours after its runner stalled, and about 19 of its 24 GPU-hours were idle. Another lost about 20 of 37 to a trainer that hung at exit. *Rule:* the GPU-idle watchdog. When every GPU is under 1 % for 45 minutes, the job ends and is recorded as a failure to read. Every job gets it, and a spec cannot opt out.

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

**Loose objects slowed every commit.** Many small commits left 24,000 loose objects. *Rule:* repack incrementally when they pass 20,000 (`git repack -d -l`: 3 minutes 24 seconds, no lock); a full `git gc` took two hours.

## People and agents

**A word is not a habit.** An approval given for one job was never meant for the next one, and a raised cap was never meant as an approval. *Rule:* one word per job, recorded verbatim with the time it was first seen, spent by the submit ([operating-model.md](operating-model.md)).

**Scaled-down qualifications underprice.** A bounded pilot priced the full run's histories 50 % low and its memory 21 % low. *Rule:* price from a run at the registered size, with a margin (1.2 times the measured wall time plus a quarter of an hour).

**A short smoke hid a counting bug.** A 32-chunk smoke test passed where the full length would have shown a 256-against-255 mismatch. *Rule:* one pass at the full registered length before a job is priced.

**Numbers from memory drift.** *Rule:* a number in a decision is computed from the files at the time of writing, and checked before it is written.
