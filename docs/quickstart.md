# Quick start

From a fresh clone to the first receipt on a real cluster.

## 1. Requirements

- A Slurm cluster with accounting enabled (`sacct` works for your account), GPU nodes, and a shared file system visible from the login nodes and the compute nodes.
- bash 4.4 or later, Python 3.9 or later. The tools use the standard library only.
- git 2.38 or later if you use the git tools (`merge-tree --write-tree`).
- Optional: `pip install nvidia-ml-py` in the environment the jobs run in, for the Hopper GPM counters (active SMs, tensor activity). Without it the launcher samples `nvidia-smi`.
- Optional: PyTorch, for the probes in `probes/`.

## 2. Put the tooling on the shared file system

Clone the repository into project space that compute nodes can read, for example `$PROJECT/ganymede`. Jobs run with the tooling folder as their working directory: logs go to `logs/`, receipts to `receipts/`.

## 3. Run the tests

```bash
bash tests/fleet_guard/run_tests.sh
```

The suite puts a fake `squeue`, `sbatch`, `scontrol` and `sacct` first on `PATH`, works on a temporary copy, and never reaches your scheduler. The last line should read `RESULT: 97 passed, 0 failed`, and its exit status is 0 (any failed check makes it 1).

## 4. Write your site file

```bash
cp slurm/site.example.env slurm/site.mysite.env
```

Fill in at least `ACCOUNT`, `PARTITION`, `GPUS_PER_NODE`, `CPUS_PER_NODE`, `MAX_TIME` (under your QoS's maximum wall time) and `ENV_ACTIVATE` (how a job activates your software environment). Set `MAX_NODES_IN_FLIGHT` to the number of nodes your fleet may hold at once and `REVIEW_THRESHOLD_HOURS` to the cost above which a human must look first. Every key is described in [configuration.md](configuration.md). `slurm/site.jupiter.env` shows the values used on JUPITER Booster.

## 5. Set the caps

```bash
cp caps.example.json caps.json
```

One entry per lane tag, in GPU-hours. A job whose name starts with a tag that has no cap is refused, and until `caps.json` exists every submission is.

## 6. Try the launcher without a scheduler

```bash
python3 pack_node.py --spec examples/specs/basic-node.json --receipt-dir /tmp/r --mode fake --fake-gpus 4 --dry-run
```

Fake mode invents GPU slots and hides every real GPU from the tasks; `--dry-run` prints the plan (which task gets which slot and cores) without starting anything. Drop `--dry-run` with a spec whose commands are harmless (`sleep 5`) to see a receipt being written.

## 7. The first real job

Edit `examples/specs/smoke-node.json`: point `cwd` at your tooling folder and list your lane's packages in `--import`. Then:

```bash
bash slurm/submit.sh --site mysite --devel examples/specs/smoke-node.json
```

The wrapper prints the job id on stdout and its reasoning on stderr. `--devel` uses the short partition and time limit from the site file.

## 8. Wait for it, then read the receipt

```bash
python3 slurm/wait_jobs.py <jobid>                    # checks every 10 minutes; returns when the queue and accounting agree the job has ended
cat receipts/infra-smoke-node-<jobid>/receipt.md
bash slurm/fleet_status.sh --site mysite              # budget, queue, waiting list, recent jobs on one screen
```

The receipt lists every task with its exit code, wall time, CPU seconds and peak memory, and every GPU with its utilisation and active SMs. Each probe's own JSON line is in `probe-<n>.out` in the same folder.

## 9. Before the agents arrive

- Read [login-nodes.md](login-nodes.md): the agents will live on the login nodes, and every heavy step there starts with `python3 tools/login_check.py`.
- Install the size hooks in the repository the agents share: `bash git-hooks/install.sh`.
- Give each agent the git tool and its lane folder: [git-for-agent-fleets.md](git-for-agent-fleets.md).
- Give each agent its instructions: `examples/agents/LANE_AGENT.md`, and to a reviewer agent `examples/agents/REVIEWER_AGENT.md`.
- Decide how the principal approves jobs: [operating-model.md](operating-model.md).
