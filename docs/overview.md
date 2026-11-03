# Overview

Ganymede is a set of small tools and a way of working for running many AI agents on one Slurm allocation. This page explains the parts and how a job moves through them.

## Concepts

**Lane.** One agent, or one team of agents, working on one line of research or development. A lane owns a folder in the shared repository, a *lane tag* (a short name such as `alpha`) and a budget. Every job a lane submits is named `<tag>-<something>`, and that prefix is how hours are charged to it.

**Principal.** The human who owns the allocation. The principal decides the caps, approves jobs, releases jobs held for review, and is the only one who may change the rules. In the original deployment this was the project's principal investigator, and the docs call the role "the principal" or "the PI".

**Reviewer.** An optional agent that sits beside the lanes. It verifies what lanes claim against their code, records and receipts, keeps the budget books and the fleet's tooling, and writes the messages the principal relays to lanes. It never submits a lane's job and never edits a lane's folder. See [operating-model.md](operating-model.md).

**Spec.** A JSON file describing one node's worth of work: the GPU tasks (one per GPU), CPU tasks and command pools. See [node-packing-and-receipts.md](node-packing-and-receipts.md).

**Site file.** `slurm/site.<name>.env`: the account, partition, node shape, time limits and the fleet's limits for one cluster. See [configuration.md](configuration.md).

**Caps.** `caps.json`: one GPU-hour cap per lane tag, required: no caps file, no submission. The caps usually sum to the allocation; a `pool` entry can hold unassigned hours. See [budget-and-accounting.md](budget-and-accounting.md).

**Worst case.** What a submission can cost at most: its time limit, as Slurm rounds it (up to whole minutes), times the GPUs it holds, times the array tasks and nodes. The budget check and the review hold both use it.

**Fleet ceiling.** The most nodes the account may have in flight at once (`MAX_NODES_IN_FLIGHT`). Slurm may not enforce a node limit on a project account; the wrapper does. A pending array counts the elements its throttle lets run at once.

**Fleet queue.** When the ceiling is reached, a new job is accepted and submitted *held*, written to the queue file, and released first in first out when a node frees. Nobody has to resubmit.

**Receipt.** `receipts/<job-name>-<job-id>/receipt.json` and `receipt.md`, written by the launcher at the end of every job: what ran, where, for how long, how it ended and what the GPUs did.

## The life of a job

1. **The lane prepares.** It writes a spec, freezes the code it will run, and works out the time limit and the worst case. On a busy fleet it also writes a one-line request for the principal: the job name, the limit, the worst case, the expected cost.
2. **The principal says yes.** One word for one named job. The lane records the word verbatim with the time it first saw it. ([operating-model.md](operating-model.md) explains why this matters.)
3. **The lane submits once.** `slurm/submit.sh --site mysite spec.json`. The wrapper takes the fleet lock, releases any queued jobs that now fit, reads the queue once, and runs its guards against that snapshot: the duplicate-name guard, the rate guard, the array rules and the ceiling. The budget check and the review threshold use the lane's caps and the job's worst case. Then it calls `sbatch` once, or refuses with an exit code that says why.
4. **The job waits or runs.** If the fleet is full, the job is held and listed in the fleet queue. `slurm/fleet_dispatch.sh` releases it, in order, when a node frees. It runs at the end of every fleet job and at the start of every submission.
5. **The node runs it.** `slurm/node.sbatch` activates the environment and starts `pack_node.py`, which assigns one GPU and a set of cores to each GPU task, runs the CPU sidecars at a lower priority, and samples the GPUs and CPUs. Slurm's warning before the time limit reaches the batch shell, which forwards it to the launcher, which forwards it to every task so they can checkpoint and exit 0. The GPU-idle watchdog ends a job whose GPUs have all been idle for 45 minutes.
6. **The receipt is written.** One JSON and one Markdown file, plus a line in `receipts/JOBS.log`. The batch script then runs the dispatcher so the next queued job can start.
7. **The lane reads it.** `slurm/wait_jobs.py` returns when the queue and accounting both agree the job has ended, and prints the receipt summary. The lane reads its results within the hour and commits the receipt.

## Design principles

- **One door.** Every job goes through `submit.sh`. A job submitted any other way has no receipt and no budget check, but its hours are still charged to the account.
- **Fail closed.** A command that cannot read the queue or the accounting does nothing. Silence is never read as "nothing is running" or "nothing was spent".
- **No retries in the tools.** There is no loop around `sbatch` and no automatic resubmission. Agents that retry are how runaway submissions start; a refusal comes back to the agent, and from there to a human.
- **Accept and hold, don't refuse.** A job that does not fit right now is queued, not refused, so the agent has no reason to come back and try again.
- **Evidence over trust.** Every claim about cost or GPU use can be checked against a receipt or accounting.
- **User space only.** No daemon, no database, no root, no changes to Slurm. Shared state lives in plain files on the shared file system, guarded by atomic `mkdir` locks.
- **Light on shared nodes.** Agents live on the login nodes. Every heavy step there looks first (`tools/login_check.py`) and stays inside what it declared ([login-nodes.md](login-nodes.md)).
- **Small and auditable.** Bash and standard-library Python, a few hundred lines per tool, every guard covered by a test against a fake Slurm.
