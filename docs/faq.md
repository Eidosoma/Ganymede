# FAQ

**Does Ganymede replace Slurm or need admin rights?**
No. Slurm stays the scheduler. Ganymede is a wrapper and a launcher that run as an ordinary user on an ordinary project account, with plain files on the shared file system as their only state.

**Why not use Slurm's own limits (QOS, association limits)?**
Use them if your site lets you. On many project accounts the users cannot create sub-accounts per agent or set node limits, and the limits that exist do not know about lanes, human sign-off, receipts or idle GPUs. Ganymede works where you have no say over Slurm's configuration, and fits beside limits you do have.

**Does it only work for AI agents?**
No. Nothing in the tools knows it is talking to an agent. They suit any group that shares an allocation, and the guards against duplicates, storms and retries help people too. They are just more necessary with agents, which retry tirelessly.

**Does it need GH200 or JUPITER?**
No. The site file describes the nodes. NUMA-local cores and the GPM counters are used where available; elsewhere the launcher falls back to the affinity mask and `nvidia-smi`. `site.jupiter.env` is one example.

**Why whole nodes only?**
The original site charged whole nodes, and a node with one busy GPU and three idle ones costs as much as a full one. The packing rule makes every job fill its node. On a site that charges per GPU you could relax it with `--allow-partial --reason`, recorded in every receipt.

**Where do the agents run?**
On the login nodes, as ordinary long-running sessions, beside everyone else's work. That makes login-node citizenship part of the job: look before you run, stay inside what you declared, keep the login GPU for short probes ([login-nodes.md](login-nodes.md)).

**How do agents talk to it?**
Through the command line, like any user: they write a spec, call `submit.sh`, wait with `wait_jobs.py`, and read the receipt. Templates for their instructions are in `examples/agents/`. A tool-server interface is on the [roadmap](../ROADMAP.md).

**Is the human sign-off enforced?**
Not by the tools yet. The review threshold holds expensive jobs automatically. The one-word-per-job protocol was enforced by the agents' instructions and checked by a reviewer agent against each job's authorisation record. A signed, single-use approval checked by `submit.sh` is the first item on the roadmap.

**What happens when the fleet is full?**
The job is accepted, submitted held and listed in the fleet queue. It starts when a node frees, in order. The agent submits once and waits.

**What should a lane do when a job fails?**
Read the receipt: which task, which exit code, what the `.err` file says, and what the GPUs were doing. A failure is a result. The next attempt is a new, priced line and a new approval, not a resubmission.

**Why is git part of this?**
Because a fleet of agents writes code and results continuously, and on a shared file system plain git becomes the bottleneck: one index lock, whole-tree scans, merges that race. The git tools were as necessary to keeping the fleet running as the Slurm tools.
