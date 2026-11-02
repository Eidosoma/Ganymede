# Node packing and receipts

`pack_node.py` runs one node's worth of work: up to one task per GPU, plus CPU tasks and command pools on the spare cores, each pinned to its own cores, with one receipt at the end. `node.sbatch` starts it inside a Slurm job; you can also run it by hand on a workstation or in fake mode for tests.

## The packing rule

A node is never held with an idle GPU. The launcher refuses a spec with fewer GPU tasks than GPU slots (exit 2) unless it is given `--allow-partial --reason "..."`, and the reason is recorded in the receipt. More GPU tasks than slots is refused too. CPU work rides beside GPU work as sidecars; a CPU-only spec on a GPU partition holds a whole node and needs the submit wrapper's `--force-node --reason`.

## The spec

```json
{
 "job": "alpha-train-node0",
 "gpu_tasks": [
  {"name": "seed-0", "cmd": "python train.py --seed 0 --resume", "cwd": "/path/to/project", "threads": 8,
   "env": {"MY_SETTING": "1"}},
  ...
 ],
 "cpu_tasks": [
  {"name": "evaluator", "cmd": "python evaluate.py", "cwd": "/path/to/project", "threads": 4, "mem_gb": 8}
 ],
 "cpu_pools": [
  {"name": "planners", "cmd_file": "jobs/sweep.txt", "cwd": "/path/to/project", "threads": 1, "parallel": 240, "mem_gb": 1.5}
 ]
}
```

| key | meaning |
|---|---|
| `job` | the job name; it starts with the lane tag (`alpha-`), which is how hours are charged |
| `gpu_tasks[]` | one task per GPU: `name`, `cmd` (run with `bash -lc`), `cwd`, `threads` (cores for this task), optional `env` |
| `cpu_tasks[]` | sidecars: the same keys, plus optional `mem_gb` (an address-space limit, `ulimit -v`) |
| `cpu_pools[]` | many small commands: `cmd_file` holds one shell command per line (blank lines and `#` comments skipped); `parallel` workers, each pinned to `threads` cores, take the lines in stride order; every command's exit code and seconds are logged; `mem_gb` per worker |
| keys starting with `_` | ignored; use `_note` for a human-readable description |

Examples are in [`examples/specs/`](../examples/specs/).

## What each task gets

| variable | value |
|---|---|
| `CUDA_VISIBLE_DEVICES` | its own GPU (empty for CPU tasks and in fake mode) |
| `PACK_SLOT`, `PACK_TASK`, `PACK_JOB`, `PACK_MODE` | its slot, task name, job name and the launcher's mode |
| `PACK_CORES` | the cores it is pinned to (it runs under `taskset`) |
| `PACK_RECEIPT_DIR` | the job's receipt folder, for small per-task records |
| `OMP_NUM_THREADS`, `MKL_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `NUMEXPR_NUM_THREADS`, `TORCH_NUM_THREADS` | the task's `threads` |
| `PYTHONDONTWRITEBYTECODE` | `1` unless already set |

Tasks should read their device from `CUDA_VISIBLE_DEVICES` (it is always device 0 inside the task), not pin GPU indices or UUIDs, and should not override the thread variables.

## Cores

- **GPU feeders first.** Each GPU task gets `--gpu-cores` cores (or its own `threads`). With `--numa`, they come from the CPU local to its GPU (read from the PCI device's `local_cpulist` in sysfs; on GH200 each GPU pairs with its own Grace CPU).
- **The highest cores of each pool.** The low cores of each processor carry the operating system, interrupts, the Slurm step, the launcher and its samplers.
- **The lowest cores are reserved.** `--reserve-cores` (2) keeps the allocation's first cores away from GPU feeders; sidecars may use them last. On GH200 a GPU task whose feeder held core 0 ran 15 to 30 % slower. The reservation is skipped when the allocation is too small for it.
- **Sidecars at low priority.** CPU tasks and pools run at `--sidecar-nice` (10), with their memory capped by `mem_gb`, so they cannot slow the feeders or push them into swap. A sidecar that imports PyTorch needs `mem_gb` of at least 8; NumPy-only sidecars run in 2 to 4.
- **Pools fill the rest.** A pool takes whatever cores are left after the tasks.

Size sidecars to end with the GPU work. The launcher refuses an idle GPU at the start, but it cannot see a job whose CPU rows outlast its GPU tasks; long CPU work belongs beside the next GPU job.

## Modes

| mode | when | GPU slots from |
|---|---|---|
| `slurm` | `SLURM_JOB_ID` is set | `SLURM_STEP_GPUS`, `SLURM_JOB_GPUS` or `CUDA_VISIBLE_DEVICES`; none if Slurm allocated none |
| `local` | no Slurm | `--gpus 0,1`, else every GPU `nvidia-smi` lists; cores from `--cores` or the affinity mask |
| `fake` | `--fake-gpus N` | N invented slots; tasks see no GPU (for testing the logic anywhere) |
| `cpu` | the spec has no GPU tasks | none |

`--dry-run` prints which task gets which slot and cores, and starts nothing.

## Signals and checkpoints

Slurm sends `SIGUSR1` `SIGNAL_BEFORE_END` seconds before the limit. The batch shell forwards it to the launcher, which forwards it to every task's process group. A task should save its state, exit 0, and support `--resume`. If every task then exits 0, the receipt says `checkpoint_segment: true` and `all_ok` stays true: the contract worked, and the next segment is a new job. `SIGTERM` (a cancel, the watchdog) is forwarded too; after `--grace` seconds (60) whatever is left is killed, and the job is a failure. Every process of a task's group gets the signal, and the task's shell waits for the running command, exits with its status and starts nothing more, so a compound command (`python train.py; echo done`) checkpoints like a single one, and so does a program started with `&` and waited for (`python train.py & wait $!`): when the signal interrupts the shell's own `wait`, the shell waits again for the program and takes its status. A program killed by signal N reads exit 128+N. Do not leave a program running in the background without waiting for it: the task would end, and the program with the job.

## The GPU-idle watchdog

With sampling on (`--sample`), when every GPU slot has read under `--idle-gpu-util` percent (1) for `--idle-gpu-minutes` (45), counted from the job's start, the launcher ends the job as if by `SIGTERM`. The receipt's `gpu_idle_watchdog` records `triggered`, `idle_since`, `idle_s` and `last_active_by_slot`; `interrupted.signal` is 15 and `all_ok` is false; the Slurm job log gets a timestamped `GPU-IDLE WATCHDOG` line. A sample counts toward the window only when every slot was read and read under the threshold: GPM for each slot, `nvidia-smi` for a slot GPM misses. A sample in which any slot could not be read counts as active, never as idle (`unread_samples` counts them). The window and threshold belong in the site file's `PACK_EXTRA`; a spec cannot change them, and `--idle-gpu-minutes 0` disables the watchdog.

A watchdog-ended job is a failure to read: a runner waiting for work that can never arrive, a trainer hung at exit, or a long CPU phase that belongs off the GPU nodes.

## The receipt

`receipt.json` and `receipt.md` in the receipt folder, beside one `.out` and `.err` per task. The main fields:

| field | meaning |
|---|---|
| `job`, `host`, `mode`, `started`, `ended`, `slurm` | what ran, where, when; the Slurm job id, name, nodes, GPUs, partition, account |
| `tasks[]` | per task: `kind`, `name`, `slot`, `cores`, `cmd`, `wall_s`, `exit`, `utime_s`, `stime_s`, `maxrss_kb`, `stdout`, `stderr` |
| `pools[]` | per pool: commands, completed, failed, workers, cores, command-seconds |
| `wall_s`, `gpu_slot_hours_held` | wall time; slots times wall time |
| `all_ok`, `checkpoint_segment`, `interrupted` | the verdict: every task exited 0 (and every pool command), whether this was a clean checkpoint segment, which signal ended it |
| `launch_error` | a task that could not be started: which one, why, and the tasks never started; the ones already started were stopped |
| `gpu_summary` | per GPU slot, means over the job: `gpu_util`, `sm_active`, `sm_occupancy`, `tensor_active`, `dram_bw`, `busy_fraction`, `peak_mem_mib`, and the `source` (`gpm` or `nvidia-smi`) |
| `gpu_imbalance` | true when the slots' busy fractions differ by 0.5 or more |
| `gpm_samples`, `gpu_samples`, `cpu_samples` | the raw samples (large; drop them when committing a receipt, as a `receipt.slim.json`) |
| `gpu_idle_watchdog` | the watchdog's settings and, if it fired, when and why |
| `cores`, `gpu_cores_per_task`, `reserved_cores`, `free_cores`, `oversubscribed` | the core plan |
| `packing_rule` | slots, GPU tasks, CPU tasks, and any `allow_partial` reason |

## Reading GPU activity

Two numbers matter, and they measure different things:

- **`gpu_util`**: the share of time any kernel was running. It is what `nvidia-smi` calls utilisation.
- **`sm_active`**: the share of the GPU's streaming multiprocessors busy while it ran, from NVML's GPM counters on Hopper. It is the quantity JSC's LLview reports as "Active SM".

A small model can show high `gpu_util` and low `sm_active`: kernels run all the time, but each uses a sliver of the card. The levers are more independent work per GPU (several processes per card, or the site's MPS through `--comment "CUDA_MPS=1"` where available), bigger batches, compiled or graph-captured kernels. On GH200 the GPM numbers were calibrated with `probes/gpu_load_probe.py` at registered duty cycles: `gpu_util` read 90.8, 49.1, 29.3 and 0.0 and `sm_active` 86.6, 44.6, 24.9 and 0.0 for duty 1.0, 0.5, 0.25 and 0.0, linear and with no cross-talk onto an idle card; for a dense BF16 matmul `sm_active` was 0.96 of `gpu_util`. Run `examples/specs/gpu-metrics-calibration.json` on a new machine before you trust the numbers there.

Without the `nvidia-ml-py` package, or on GPUs without GPM, the summary comes from `nvidia-smi` samples and says so (`source: nvidia-smi`, no `sm_active`).

## Options

| option | default | meaning |
|---|---|---|
| `--spec`, `--receipt-dir` | required | the spec; where the receipt goes |
| `--mode` | auto | `auto`, `slurm`, `local`, `fake`, `cpu` |
| `--gpus` | | local mode: GPU indices |
| `--fake-gpus N` | 0 | fake mode with N slots |
| `--cores` | affinity mask | cores to use, e.g. `6-11,17-23` |
| `--gpu-cores N` | split evenly | cores per GPU task |
| `--numa` | off | NUMA-local feeder cores |
| `--reserve-cores N` | 2 | lowest cores kept from GPU feeders |
| `--allow-partial --reason` | off | allow fewer GPU tasks than slots |
| `--nice N`, `--sidecar-nice N` | 0, 10 | priorities for GPU tasks and for sidecars |
| `--sample S` | 0 (off) | GPU sampling period in seconds; the site default is 30 |
| `--cpu-sample S` | 0 (off) | CPU busy-fraction sampling for feeder and sidecar cores |
| `--idle-gpu-minutes M`, `--idle-gpu-util P` | 45, 1 | the watchdog (needs `--sample`; 0 minutes disables it) |
| `--grace S` | 60 | seconds between `SIGTERM` and `SIGKILL` |
| `--dry-run` | | print the plan, start nothing |

Exit code: 0 if every task exited 0 (a clean checkpoint segment included), 1 otherwise, 2 for a refused spec (the packing rule, or a task folder that does not exist: checked before anything starts).
