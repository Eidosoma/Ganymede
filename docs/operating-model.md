# The operating model

The tools stop the worst mistakes. The operating model is what made a fleet of agents trustworthy enough to leave running: who may do what, how a human approves a job, and what gets written down. This page describes how the original fleet on JUPITER ran. Take what fits your setting; the tools do not enforce most of it (see [ROADMAP.md](../ROADMAP.md) for making the sign-off enforceable).

## Roles

| role | who | may | may not |
|---|---|---|---|
| **Principal** | a human; owns the allocation | approve jobs, set and move caps, release held jobs, change the rules, cancel jobs | |
| **Lane** | an agent (or a team of agents) with one folder and one tag | design, register, freeze and run its own experiments; submit its own approved jobs; read its own receipts; commit its own folder | submit without approval; edit another lane's folder; change the tooling; clear a hold |
| **Reviewer** | an agent beside the lanes (optional, recommended) | read everything; verify claims against code, receipts and accounting; keep caps, the ledger and the tooling; write relay messages; record decisions | submit, release or cancel a lane's job; edit a lane's folder; act on its own verdicts without the principal |

The reviewer is what lets one human oversee a dozen lanes. It reads a lane's result before the human does, checks the numbers against the files, and gives the principal a short, plain verdict with a recommendation. Two reviewers can split the lanes, one reviewer per milestone; budget and tooling stay with one of them.

## One word, one job

The rule that matters most: **a human approves each job, and the approval covers that one job only.**

1. **The lane writes one submit line.** Job name, time limit, worst-case GPU-hours, expected GPU-hours, and anything unusual (several nodes, a shared-GPU mode, card activity below the fleet's targets). See [`examples/agents/submit-line.example.md`](../examples/agents/submit-line.example.md).
2. **The principal answers.** "Go", "lets go", "yes on the smoke" or any clear word. The word covers the one job it was asked for. A raised cap is not a word, and a word does not carry over to a second job, a retry or a successor.
3. **The lane records the word before it submits.** Verbatim, with the time it was *first observed*: the first reading of the clock (`date -u`) after the message arrived, never an estimate. The record also holds the submit line, the frozen code's hashes, and the checks just before submitting: the controller answers, accounting shows no job of that name already, the queue is readable, the cap has room. See [`examples/agents/authorised_by.example.json`](../examples/agents/authorised_by.example.json).
4. **The lane submits once.** Through `slurm/submit.sh`, exactly once. The word is spent by the submit.
5. **A refusal goes back to the human.** If the wrapper refuses, or `sbatch` fails, the lane reports and stops. It does not retry. The principal may explicitly permit one retry; that permission is recorded beside the word.
6. **One watcher, slow polling.** The lane waits with one watcher per job, polling every 20 minutes or so (`wait_jobs.py --poll 1200`). No loops on Slurm commands faster than every ten minutes, and no Slurm queries from inside a job.
7. **The result is read within the hour.** The lane reads the receipt and its outputs, writes what happened (including failures, in full), and commits the receipt. A failed job is a result to read, not something to resubmit; the next attempt is a new line and a new word.

Why so strict? Because the failure modes of agents are loops and drift. An agent with standing permission to "keep the GPUs busy" will eventually spend the allocation on a bug. An agent with one word per job cannot spend more than one job's worst case without a human seeing it.

## Pricing a job

- **Measure, then price.** A time limit is at least 1.2 times the measured wall time plus a quarter of an hour, from a qualification run at the registered size. A short or scaled-down qualification underprices the full run; in one lane a bounded pilot priced the histories 50 % low and the memory 21 % low.
- **No limit under 20 minutes.** Slurm's checkpoint warning arrives 15 minutes before the limit (`SIGNAL_BEFORE_END`); a shorter job would be warned almost as it starts.
- **State the worst case.** The time limit times the GPUs held. That is what the budget check uses and what the principal approves.
- **Respect the maximum wall time.** On JUPITER a `--time` over 12 hours is refused at `sbatch`, after the word was spent. Longer work runs as checkpointed segments.
- **Card activity targets.** The original fleet asked for at least 70 % GPU utilisation and 50 % active SMs over a job, and for a stated exception when a job could not meet them. The receipt shows what was achieved.

## Budget

- **Caps sum to the allocation.** Each lane has a cap in GPU-hours; a `pool` entry holds what is not yet assigned.
- **Moving hours is a decision, not a side effect.** The principal moves hours between lanes; the reviewer edits `caps.json` and records the move with its reason and the principal's words. Hours lent to a lane are written down so they can be returned.
- **Holds and parks.** A lane can be put on hold (its cap set to what it has used) without losing its place; a parked line waits for new hours.
- **Count from accounting, not from memory.** The reviewer reads `budget.py` before every decision and writes numbers only after computing them.

## What gets written down

- **A results log**, kept by the reviewer: one entry per decision, newest first. Each entry carries the human's words verbatim with their observed times, what was checked and how, the numbers (computed, not remembered), the ruling, a relay message for the lanes, and predictions with probabilities, so that later reviews can be scored.
- **Receipts**, committed by the lanes. Receipt files over 256 KB are committed as a slimmed copy (the raw sampler arrays dropped), and the full file stays on disk.
- **The plan before the run.** A check's reading rules and thresholds are committed before the run starts, so a result cannot quietly change what it is measured against.

## Hygiene that kept the fleet healthy

- **Time stamps from the clock.** `date -u` at the moment of writing. Slurm prints local time unless `TZ=UTC` is set; write `TZ=UTC sacct ...` everywhere.
- **Check accounting by name before any resubmission after an outage.** A job accepted in the controller's last seconds can reappear when it comes back.
- **Look before you run on a login node.** The agents live on shared login nodes, and their processes count against one per-user limit there. Before any suite, build, CPU stage or login-GPU probe a lane runs `tools/login_check.py` and runs with what it allows: the least-busy cores (never 0 and 1), 12 workers under a 5-minute load of 30 or else 6, `nice`, CUDA hidden for CPU work, its own memory under about 20 GiB, at most 16 GiB on the login GPU while others use it. See [login-nodes.md](login-nodes.md).
- **Caches and outputs out of home.** Home directories have small quotas. Point `HF_HOME`, `TORCH_HOME` and `PIP_CACHE_DIR` at project or scratch space, and keep agent tool state in mind; it filled a home quota once.
- **Files with many small parts stay on scratch.** Project file systems often limit the number of files (inodes) more tightly than bytes. Keep code, protocols, receipts and one archive per job in the project; runner state, per-sample outputs and checkpoints go to scratch.
- **Read-only on other lanes.** A lane never writes into another lane's folder. The reviewer never writes into any lane's folder.
