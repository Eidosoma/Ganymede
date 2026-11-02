# Being a good citizen on the login nodes

Agents live on the login nodes. On JUPITER a dozen agent sessions ran there for weeks beside every other user of the machine: editing, testing, building, probing the login GPU, committing, watching jobs. A login node is shared, its limits are per user across all your sessions, and when it falls over, everyone on it loses their work, other projects included. This page is how the original fleet kept its footprint small. The rule underneath all of it: **look before you run, and stay inside what you declared.**

## Look before you run

Before anything heavier than an editor or a git command (a test suite, a build, a CPU stage, a probe on the login GPU), check the node:

```bash
python3 tools/login_check.py --want-cpus 12                    # CPU work: how many workers, on which cores
python3 tools/login_check.py --want-cpus 6 --mem-gib 10         # with the host memory the run will use
python3 tools/login_check.py --want-cpus 6 --gpu-gib 12         # with a probe on the login GPU
```

```
load 75.0 106.0 112.7 on 72 cores; 49 under 50 % busy; 261.7 GiB available
your sessions: 1.0 GiB of 143.1 GiB allowed, peak 3.3 GiB, 44 tasks
  5-minute load 106.0 is at or above 30.0: 6 workers, not 12
REDUCE: 6 worker(s) on cores 11,21,28-29,43,56
run with: taskset -c 11,21,28-29,43,56 nice -n 10 env CUDA_VISIBLE_DEVICES= <command>
```

It reads (and never changes) the load, how busy each core was over the last second, the free memory, your own sessions' memory against your per-user limit, and with `--gpu-gib` the login GPU's memory and the other users' processes on it. It answers **GO** (exit 0), **REDUCE** (exit 1: run with the smaller settings it gives) or **WAIT** (exit 2: do not start now), and the prefix to run with. In scripts:

```bash
CORES=$(python3 tools/login_check.py --want-cpus 8 --print-cores); rc=$?   # a plain list: 11,21,28,29,43,56
[ "$rc" -le 1 ] || { echo "login node too busy: not starting"; exit 0; }
WORKERS=$(echo "$CORES" | tr ',' '\n' | wc -l)
taskset -c "$CORES" nice -n 10 env CUDA_VISIBLE_DEVICES= ./my_test_runner --workers "$WORKERS"
```

Without the tool, the same reading by hand:

```bash
cat /proc/loadavg                                     # the 1-, 5- and 15-minute load
systemctl status user-$(id -u).slice | grep -E 'Memory|Tasks'   # your sessions' memory and tasks against the per-user limit
free -g                                               # the node's free memory
nvidia-smi                                            # the login GPU: memory used and whose processes
```

## The rules the fleet kept

**CPU**

- **Pick the least-busy cores, never cores 0 and 1.** Other users and the operating system kept the first cores of JUPITER's login nodes 86 to 100 % busy; tests pinned there ran twice as slow and timed out. Pin with `taskset -c` to cores measured idle at launch.
- **Size to the load.** 12 workers when the 5-minute load is under 30, else 6; at least 6 for a test suite (fewer makes a suite take hours, which holds cores longer); not at all when fewer than 6 cores are idle.
- **Low priority, no GPU by accident.** `nice -n 10` for anything that runs more than a minute, and `CUDA_VISIBLE_DEVICES=` for CPU checks, so a test cannot wander onto the shared GPU.
- **Short invocations.** Each test invocation under five minutes. Anything longer runs detached (`setsid nohup`), with its PID, host, paths and a status command written down, within declared threads, memory and wall time.
- **Experiments run on the compute nodes.** The login node is for editing, tests, builds, qualification and short probes. A CPU-serial stage that must run there runs detached, on one or a few cores, under `nice`, with a memory cap, in bounded segments, on the principal's word.

**Memory**

- **Your sessions share one limit.** On JUPITER every user's processes on a login node share one memory limit (143 GiB per user at the time), and processes are killed when it is reached; it happened once, at 138 GiB. Keep your own footprint under about 20 GiB and check the slice before anything large.
- **Cap what you start.** `ulimit -v` (or a memory limit in the launcher) on detached stages and sidecars, so one runaway process cannot take the whole slice.
- **The node can fail for others' reasons.** Twice the login node ran out of memory because of other users' processes. The parallel file system faltered, and every agent whose program files sat on it died. If that happens, read `dmesg -T | grep -i "out of memory"` and your slice's `memory.events` before touching anything, then re-arm one watcher per job and resubmit nothing.

**The login GPU**

- **It is shared, and people arrive mid-run.** Check `nvidia-smi` before you start. While anyone else uses the card, keep each of your items at or under 16 GiB for the whole run. A launch-time check is not enough: on one evening four runs ended up over the limit while others used the card, and three of them had started on a free card.
- **Watch it while you run, and stop everything you started.** A watchdog that ends your item when the card fills must end every process of the item, including workers started in their own sessions (MPS clients), and any daemon you started.
- **Probes and qualification only.** Science runs on the fleet. Record each login-GPU item's start and end, the card's other load, and its utilisation, and count login GPU-hours apart from the allocation.

**Processes**

- **Stop by explicit PID.** Never `pgrep -f` or `pkill -f` on a name that also appears in your own command line: it matches the calling shell. Under one shared user, a name match can also hit another lane's processes.
- **Clean up the tree.** `timeout` puts its child in a new process group, so killing a runner's group leaves its units running. List and stop the workers and their children by PID, then check that nothing is left.
- **No tight loops.** One watcher per job, polling every 20 minutes or so (`wait_jobs.py --poll 1200`). No loop on any Slurm command faster than every ten minutes. Budget and accounting queries are heavy on the scheduler's database; run them when you decide something, not in a loop.

**Files**

- **No whole-tree scans of the shared file system.** No `git status` or `find` over the whole repository, no `git gc` (use the git tools: [git-for-agent-fleets.md](git-for-agent-fleets.md)).
- **/tmp is local to the node.** It is lost when your session moves to another login node and shared with every user there. Clean up what you leave, and keep anything you need later on the shared file system.
- **Caches and outputs out of home.** Set `HF_HOME`, `TORCH_HOME` and `PIP_CACHE_DIR` to project or scratch space; agent tool state filled a 21 GB home quota once.

## For the reviewer

At each status check, also look at the fleet's footprint on the login nodes you can see: your user's processes and memory (`systemctl status user-$(id -u).slice`), long-running processes by lane, anything on the login GPU, and any leftovers in `/tmp`. Note overlaps on the login GPU and anything over the declared bounds, and tell the principal.
