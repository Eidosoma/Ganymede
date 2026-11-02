# Configuration

## Site files

`slurm/site.<name>.env`, selected with `--site <name>`. The file is sourced by bash, so it must be valid shell, and parsed by `budget.py` as `KEY=VALUE` lines (text after `#` is ignored). Three are provided: `site.example.env` (a template), `site.jupiter.env` (JUPITER Booster) and `site.local.env` (a workstation stand-in).

| key | example | used by | meaning |
|---|---|---|---|
| `ACCOUNT` | `"my-project"` | submit, dispatch | the Slurm account every fleet job is charged to |
| `PARTITION` | `"booster"` | submit | the GPU partition |
| `DEVEL_PARTITION` | `"booster"` | submit | the partition for `--devel` |
| `GPUS_PER_NODE` | `4` | submit, budget | GPUs per node; every job asks for all of them |
| `CPUS_PER_NODE` | `288` | submit | cores asked for per node |
| `MAX_TIME` | `"11:30:00"` | submit | the default `--time`; keep it under the QoS maximum |
| `DEVEL_MAX_TIME` | `"00:30:00"` | submit | the default `--time` for `--devel` |
| `SIGNAL_BEFORE_END` | `900` | submit | seconds before the limit at which Slurm sends `USR1` |
| `ENV_ACTIVATE` | `'source /path/env/activate.sh'` | node.sbatch | evaluated before the launcher starts |
| `PACK_EXTRA` | `"--numa --sample 30 ..."` | node.sbatch | default launcher options for every job (the watchdog's window and threshold belong here) |
| `CPU_PARTITION` | `""` | submit | a partition for `--cpu-only` specs; empty means CPU work rides as sidecars |
| `BUDGET_SINCE` | `"2026-09-18"` | budget | the start of the allocation period |
| `BUDGET_WINDOW_DAYS` | `7` | budget | the accounting read goes in UTC windows of this many days |
| `BUDGET_CACHE_FILE` | | budget | where finished windows are cached (default `receipts/.budget_cache.<site>.json`) |
| `CHARGE_WHOLE_NODES` | `1` | budget | charge a job its nodes times `GPUS_PER_NODE` when accounting shows no GPU entry |
| `MAX_NODES_IN_FLIGHT` | `5` | submit, dispatch | the fleet ceiling; unset means no ceiling and no queue |
| `REVIEW_THRESHOLD_HOURS` | `50` | submit | a worst case above this is held for review; `0` disables |
| `NO_DUPLICATE_NAMES` | `1` | submit | refuse a name that is already pending or running |
| `MAX_SUBMITS_PER_10MIN` | `12` | submit | the rate guard |
| `FLEET_QUEUE_FILE` | `"${FLEET_QUEUE_FILE:-/shared/receipts/FLEET_QUEUE.log}"` | submit, dispatch | the fleet queue (default `receipts/FLEET_QUEUE.log` beside the tooling) |
| `FLEET_LOCK_DIR` | `"${FLEET_LOCK_DIR:-/shared/receipts/.fleet.lock}"` | submit, dispatch | the fleet lock |
| `FLEET_DISPATCH_LOG` | `"${FLEET_DISPATCH_LOG:-/shared/receipts/FLEET_DISPATCH.log}"` | dispatch | the release log |

The duplicate, rate and ceiling guards run only when `squeue` is available and at least one of `MAX_NODES_IN_FLIGHT`, `NO_DUPLICATE_NAMES` and `MAX_SUBMITS_PER_10MIN` is set.

## Files

| file | meaning |
|---|---|
| `caps.json` | GPU-hour caps per lane tag ([budget-and-accounting.md](budget-and-accounting.md)) |
| `receipts/<name>-<jobid>/` | one folder per job: `receipt.json`, `receipt.md`, one `.out` and `.err` per task |
| `receipts/JOBS.log` | one line per job start and end: `START`, `DONE`, `FAILED`, `REFUSED` |
| `receipts/FLEET_QUEUE.log`, `FLEET_DISPATCH.log` | the fleet queue and the dispatcher's releases (unless the site file moves them) |
| `receipts/.budget_cache.<site>.json` | the budget check's finished accounting windows; rebuilt by itself when absent |
| `logs/<name>-<jobid>.out`, `.err` | Slurm's output for the batch script and the launcher |

## Environment variables

| variable | used by | meaning |
|---|---|---|
| `FLEET_LOCK_WAIT` | submit | seconds to wait for the fleet lock (45) |
| `FLEET_QUEUE_FILE`, `FLEET_LOCK_DIR`, `FLEET_DISPATCH_LOG` | submit, dispatch | override the site file (the tests use this) |
| `LANEGIT_ROOT` | lanegit | the shared checkout (default: the one you are in) |
| `LANEGIT_LANES_PREFIX` | lanegit | the folder lane folders live in, for `check` |
| `LANEGIT_INDEX_WAIT` | lanegit | seconds to wait for a busy index lock before deferring (120) |
| `FLEET_GIT_TOKEN_FILE` | lanegit, ffscoped | an optional token file for HTTPS, read only by git's credential helper |
| `FLEET_MAX_NEW_FILE_KB`, `FLEET_MAX_FILE_MB` | lanegit, hooks | size limits for new files (256 KB) and any file (10 MB) |
| `FLEET_ALLOW_LARGE` | lanegit, hooks | `1` lifts the size limits once |
| `FF_ROOT`, `FF_REMOTE` | ffscoped | the checkout and the remote |
| `PACK_FAKE_GPU_UTIL` | pack_node | tests only: every fake slot samples this utilisation |
| `FLEET_RUNS` | probes/nccl_test.py | where the test checkpoint goes |
| `FLEET_GPUS_PER_NODE` | probes/nccl_test.py | GPUs per node for rank arithmetic (4) |

## Lane tags

A job's lane is the part of its name before the first `-`. Choose tags without dashes (`alpha`, `beta`, `infra`), give each one a cap, and make every job name start with its lane's tag. That one convention is how hours, queue entries and receipts are attributed.
