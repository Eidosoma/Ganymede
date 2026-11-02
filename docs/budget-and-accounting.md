# Budget and accounting

A fleet of agents spends a shared allocation. Ganymede keeps the spending per lane visible and bounded, from Slurm's own accounting.

## Caps

`caps.json`, beside the tooling:

```json
{"alpha": 1200, "beta": 800, "gamma": 500, "infra": 100, "pool": 400}
```

One entry per lane tag, in GPU-hours. A job's lane is the part of its name before the first `-`. A job whose tag has no cap is refused, so a misnamed job cannot run uncapped. The caps usually sum to the allocation; a `pool` entry holds hours not yet assigned. Moving hours between lanes is an edit to this file, and a decision the principal makes ([operating-model.md](operating-model.md#budget)).

## The budget check

```
python3 slurm/budget.py --site mysite                         # the table
python3 slurm/budget.py --site mysite --json                  # the same, as JSON
python3 slurm/budget.py --site mysite --lane alpha --est-hours 12   # exit 3 if this job would take alpha over its cap
```

```
lane          used  committed     cap  headroom  R/PD
alpha       875.40       9.00    1200   315.600  1/0
beta        412.10       0.00     800   387.900  0/0
```

- **used**: GPU-hours from `sacct` since `BUDGET_SINCE`, for the site's `ACCOUNT` only: each job's GPUs times its elapsed time. GPUs come from `AllocTRES` (`gres/gpu=N`); on whole-node sites (`CHARGE_WHOLE_NODES=1`) a job with no GPU entry is charged its nodes times `GPUS_PER_NODE`.
- **committed**: from `squeue`: a running job's remaining time, a pending job's full time limit, times its GPUs.
- **headroom**: cap minus used minus committed.

`submit.sh` calls the check with the job's worst case before every submission. If `sacct` or `squeue` fails or exits non-zero, the check refuses (exit 3) rather than read the silence as "nothing used".

**Light on the scheduler.** Every submission runs this check, so it has to be cheap for the accounting database that every user of the machine shares:

- It reads the site's account only (`sacct -a -A <ACCOUNT>`: every user of the account, nobody else's jobs, which also keeps other projects' job names out of your lanes on sites where users can see each other's jobs).
- It reads the period in UTC windows of `BUDGET_WINDOW_DAYS` (7), and counts each job once however many windows return it. One reply for the whole period grows with the fleet; on JUPITER replies of about 1.8 MB inside the accounting database were cut off intermittently, with exit code 1 and empty output.
- A window that ended a day ago or more, with every job in it finished, never changes again. It is kept in `receipts/.budget_cache.<site>.json` and not read again, so a check reads only the current week or two. A window whose read failed is never cached; `--no-cache` reads everything again.

On the original fleet's account (about 1,000 jobs over six weeks) the whole-period read took 5 to 8 seconds of database work on every submit. With windows and the cache, the first check took 14 seconds (it reads every window once and caches the finished ones) and the next one 0.66 seconds, with the same hours in every lane; after the first, a check reads only the current week.

One more thing to know: `sacct -a` shows only the jobs your site lets you see. On JUPITER that is your own user's jobs, so the original fleet ran every agent under one user.

## The ledger

```
python3 slurm/ledger_from_sacct.py --since 2026-09-18 --account my-project --caps caps.json --ledger ALLOCATION_LEDGER.md --gpus-per-node 4 --charge-node-gpus
```

Appends one row per new job to a Markdown table (job, lane, name, partition, nodes, GPUs, cores, elapsed seconds, GPU-hours, core-hours, state, start and end in UTC) and prints totals per lane against the caps. It is idempotent per job id, and a row first written while a job was still running is replaced with the final accounting once it ends. `--account` reads one account only, and the read goes in UTC windows of `--window-days` (7), each job once. `--sacct-file` reads a saved dump instead of calling `sacct`.

## The site's own view

A site's quota tool may count differently and lag behind accounting. On JSC systems `jutil project cpuquota` reports core-hours (on JUPITER Booster, 72 core-hours make one GPU-hour), and its figure trailed `sacct` by about a day. Compare the two after a quiet day, not in the middle of a busy one.

## Status screens

```bash
bash slurm/fleet_status.sh --site mysite --hours 24     # budget, queue, waiting list, finished jobs, receipts: one screen
bash slurm/jobs.sh alpha --since 2026-10-01             # one lane's jobs in the queue, in accounting, and in the job log
python3 slurm/wait_jobs.py 1234567 1234568 --poll 1200  # block until these jobs have ended, then summarise their receipts
```

`wait_jobs.py` returns only when the queue no longer lists the jobs *and* accounting shows a terminal state for each. A failed `squeue` never counts as an empty queue, because a waiter that returns early lets an agent start the next segment beside the first. It checks every 10 minutes by default (`--poll`) and never faster than once a minute: one watcher per job, polling slowly, is the fleet's rule for a scheduler everyone shares. Exit 0 means every job completed with `all_ok`; anything else is 1.

## Getting results home

`slurm/copyback.sh <user@host> <remote-root> <local-root> <list-file>` copies exactly the paths in the list with checksums and writes a SHA-256 manifest. Scratch file systems are often purged after weeks of inactivity; the list is what survives.
