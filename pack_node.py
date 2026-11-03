#!/usr/bin/env python3
"""pack_node.py: run up to N single-GPU tasks plus CPU sidecar tasks on ONE node, one GPU per task, cores pinned, one receipt.

Fleet packing rule: a node is never held with an idle GPU. The launcher refuses a spec with fewer GPU tasks than GPU slots
unless --allow-partial is given with a written reason (recorded in the receipt).

Modes (detected, or forced with --mode):
  slurm  inside an sbatch/salloc allocation: GPU slots from SLURM_JOB_GPUS / SLURM_STEP_GPUS / CUDA_VISIBLE_DEVICES, cores from the
         task's affinity mask (sched_getaffinity), node name from SLURM.
  local  no Slurm: GPU slots from --gpus (default: every GPU nvidia-smi lists), cores from --cores or the affinity mask.
  fake   --fake-gpus N: N slots with CUDA_VISIBLE_DEVICES="" (tasks see no GPU); for testing the packing logic on a busy machine.
  cpu    a spec with no gpu_tasks: zero GPU slots, CPU tasks pinned to the job's cores. On a GPU partition this still holds the
         node; the submit wrapper decides whether the site allows it (see --cpu-only and --force-node there).

Spec (JSON):
  {"job": "alpha-bank-node0",
   "gpu_tasks": [{"name": "founder-0", "cmd": "python train.py --seed 0", "cwd": "/path", "env": {"X": "1"}, "threads": 8}, ...],
   "cpu_tasks": [{"name": "audit", "cmd": "python audit.py", "cwd": "/path", "threads": 4}, ...]}
CPU pools: "cpu_pools": [{"name": "planners", "cmd_file": "cmds.txt", "threads": 1, "parallel": 64, "cwd": "...", "mem_gb": 4}]
runs the commands of a file (one per line) through a pool of `parallel` workers pinned one core each on the sidecar cores,
recording per-command exit codes; this is how a node's spare cores are filled with hundreds of small jobs. Under the checkpoint
signal (SIGUSR1, and SIGTERM) a worker starts nothing more, waits for its running command and records its status, marked
interrupted; the launcher writes the commands not finished, interrupted and never started, in file order, to
<pool>.resume.cmds in the receipt folder: the next segment's pool runs that file. Under SIGUSR1 a pool counts towards the
checkpoint segment when no command it finished had failed: an interrupted command is kept, not judged (its status is in
the receipt), so pool commands must be safe to run again.
Sidecars (cpu_tasks and pools) run at --sidecar-nice (default 10) so GPU feeders win any contention; a task's "mem_gb" sets an
address-space limit (ulimit -v) so a sidecar cannot push a GPU feeder into swap. --gpu-cores reserves cores per GPU task
(JUPITER: 8 of 72 per socket, NUMA-local with --numa); everything else is sidecar territory.
Each task gets: CUDA_VISIBLE_DEVICES (its GPU, or "" in fake mode), PACK_SLOT, PACK_TASK, PACK_JOB, PACK_CORES, PACK_RECEIPT_DIR (the job's receipt folder, for small per-task records),
OMP_NUM_THREADS / MKL_NUM_THREADS / OPENBLAS_NUM_THREADS / NUMEXPR_NUM_THREADS / TORCH_NUM_THREADS = threads, and is pinned with taskset to its cores.
Receipt: <receipt-dir>/receipt.json and receipt.md (per-task wall, exit code, user/system CPU seconds, max RSS, checkpoint_segment when SIGUSR1 was forwarded and every task then exited 0 and no pool command had failed,
GPU-slot-hours held = slots * wall / 3600, optional nvidia-smi samples). Exit code: 0 if every task exited 0, else 1;
2 for a refused spec.
Signals: SIGUSR1 (Slurm's --signal warning before the wall limit) and SIGTERM are forwarded to every task, then the launcher
waits --grace seconds and terminates what is left; the receipt records the interruption. The task shell outlives the forwarded
signal: it waits for the running command, exits with that command's status and starts nothing more, so a compound command
("python train.py; echo done") checkpoints like a single one; a program killed by signal N reads exit 128+N. A task whose folder
is missing is refused before anything starts; a task that cannot be started stops the ones already started, and the receipt
records it (launch_error).
GPU-idle watchdog (every job; a spec cannot opt out): when every GPU slot has
read under --idle-gpu-util percent utilisation for --idle-gpu-minutes (default 1 % for 45 minutes, from the job's start), the
launcher ends the job as if by SIGTERM; the receipt says so (gpu_idle_watchdog: triggered, idle_since, idle_s, last_active_by_slot) and
the launcher prints a timestamped GPU-IDLE WATCHDOG line to its stdout (the Slurm job log logs/<name>-<jobid>.out); a sample that cannot be read counts
as active, and so does a sample in which any one slot cannot be read. --idle-gpu-minutes 0 disables it (the site file's choice, never a spec's).
"""
import argparse, json, math, os, shlex, signal, socket, subprocess, sys, threading, time
from datetime import datetime, timezone

def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')

# The task shell outlives the forwarded signal. SIGUSR1 and SIGTERM go to the task's whole process group; a shell that is still a
# parent (a compound command such as "python train.py; echo done") used to die of the signal at once, so the launcher stopped waiting
# while the program was still saving. With this trap the shell waits for the running command, then exits with its status and starts
# nothing more. (A program killed by signal N reads exit 128+N; a single command that the shell used to replace read -N.)
# A program started with & and waited for (`program & wait $!`): a trapped signal makes bash's own wait return 128+N, and when the
# program ends in that same moment (a save that takes no time) bash can lose the program's status: a second wait then answers -1 or
# blocks for good, and a liveness test before the second wait keeps the interrupted 128+N instead (a clean, immediate save then
# reads as a failed job). So the task shell has its own `wait`: for its
# own background programs it checks every 50 ms whether they still run (a timed read on a pipe it holds open, no process started)
# and calls bash's wait only once they have ended, when the status is collected at once; the trap does the same for what the shell
# was waiting for, then for every other background program. A program that has ended but is not reaped yet (a zombie in /proc,
# which kill -0 still finds) counts as ended; the job list is taken in a subshell that ignores the two signals; and inside the trap
# the pause is `sleep 0.05`, not a timed read nested in the interrupted one. Its answers are bash's (the last pid's status; 0 for a plain wait; 127
# for a pid that is not a child), also under set -e. With an option (-n) or a job spec (%1) it is bash's wait itself.
# A child of the shell that inherited the trap (forked for `program &` and signalled before it became the program) resets it and dies
# of the signal, as it would without the trap: it must never answer with the trap's status (it once exited 0, read as a success).
TASK_SHELL = r'''__pk_main=$BASHPID
__pk_nap() { if [ -n "${__pk_fd:-}" ] && [ -z "${__pk_in:-}" ]; then read -r -t 0.05 -u "$__pk_fd" __pk_x || :; else sleep 0.05 || :; fi; }
__pk_live() { local s; kill -0 "$1" 2>/dev/null || return 1; { read -r s < "/proc/$1/stat"; } 2>/dev/null || return 0; s=${s##*) }; [ "${s%% *}" != Z ]; }
__pk_wait() { local IFS=$' \t\n' p s=0 j=$'\n'$(trap '' USR1 TERM; jobs -p)$'\n'; for p; do if [[ $j == *$'\n'$p$'\n'* ]]; then while __pk_live "$p"; do __pk_nap; done; fi; builtin wait "$p" && s=0 || s=$?; done; return $s; }
wait() { local IFS=$' \t\n' r; case " $* " in *" -"*|*%*) builtin wait "$@"; return;; esac; if [ $# -eq 0 ]; then __pk_w=$(jobs -p); __pk_all=1; else __pk_w="$*"; __pk_all=; fi; __pk_wait $__pk_w && r=0 || r=$?; __pk_w=; [ -z "$__pk_all" ] || r=0; return $r; }
__pk_stop() { local IFS=$' \t\n' sig=$1 rc=$2; if [ "$BASHPID" != "$__pk_main" ]; then trap - "$sig"; kill -s "$sig" "$BASHPID"; exit 1; fi; __pk_in=1; if [ -n "${__pk_w:-}" ]; then __pk_wait $__pk_w && rc=0 || rc=$?; [ -z "$__pk_all" ] || rc=0; fi; __pk_wait $(jobs -p) || :; exit "$rc"; }
trap '__pk_stop USR1 $?' USR1
trap '__pk_stop TERM $?' TERM
__pk_f=$(mktemp -u "${TMPDIR:-/tmp}/.pk_nap.XXXXXX" 2>/dev/null) && mkfifo -m 600 "$__pk_f" 2>/dev/null && exec {__pk_fd}<>"$__pk_f"; rm -f "$__pk_f" 2>/dev/null
'''

def slot_readings(slot_ids, gpm, smi):
    """One utilisation per expected slot: GPM's where it is a finite number, else nvidia-smi's for that slot, else None (not read).
    The idle watchdog counts a sample with any slot not read as active: missing is not idle."""
    out = {}
    for s in slot_ids:
        v = gpm.get(s)
        if not (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)):
            v = smi.get(s)
        out[s] = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None
    return out

POOL_WORKER = r"""#!/usr/bin/env python3
# pool worker: runs commands i, i+parallel, i+2*parallel, ... of a command file; one line per command in the log: index rc seconds,
# and ' interrupted' when the checkpoint signal (SIGUSR1) or SIGTERM came while it ran. The signal reaches the worker's whole process
# group, its running command too: the worker then starts nothing more, waits for that command and records its status (a worker
# that died of the signal at once let the launcher return while its command was still saving).
import signal, subprocess, sys, time
cmdf, w, par, logf = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
cmds = [l.rstrip('\n') for l in open(cmdf) if l.strip() and not l.startswith('#')]
stop = []
def halt(sig, frame):
    stop.append(sig)   # a plain flag: no lock and no I/O in a signal handler
signal.signal(signal.SIGUSR1, halt); signal.signal(signal.SIGTERM, halt)
with open(logf, 'a') as log, open(logf + '.err', 'a') as err:
    for i in range(w, len(cmds), par):
        if stop:
            break
        t0 = time.time(); n = len(stop)
        rc = subprocess.call(cmds[i], shell=True, stdout=subprocess.DEVNULL, stderr=err)
        log.write(f"{i} {rc} {time.time()-t0:.3f}" + (" interrupted" if len(stop) > n else "") + "\n"); log.flush()
"""

def parse_cores(spec):
    out=[]
    for part in str(spec).split(','):
        part=part.strip()
        if not part: continue
        if '-' in part:
            a,b=part.split('-'); out.extend(range(int(a),int(b)+1))
        else: out.append(int(part))
    return sorted(set(out))

def nvidia_gpus():
    try:
        r=subprocess.run(['nvidia-smi','--query-gpu=index,pci.bus_id','--format=csv,noheader'],capture_output=True,text=True,timeout=20)
        rows=[l.split(',') for l in r.stdout.strip().splitlines() if l.strip()]
        return [(r[0].strip(), r[1].strip()) for r in rows]
    except Exception:
        return []

def local_cpulist(bus_id):
    """Cores local to a GPU's NUMA node (GH200: the Grace CPU of the same superchip). None if the sysfs entry is absent."""
    b=bus_id.lower()
    if len(b)>12: b=b[-12:]
    p=f'/sys/bus/pci/devices/{b}/local_cpulist'
    try:
        s=open(p).read().strip()
        cores=parse_cores(s)
        return cores if cores else None
    except Exception:
        return None

class GpmSampler:
    """Hopper GPM counters through NVML (no daemon): each call returns, per GPU, the averages over the interval since the previous
    call: graphics_util (share of time any kernel ran; nvidia-smi's utilization.gpu), sm_active (share of SMs busy while the GPU
    ran, the quantity JSC's LLview reports as Active SM), sm_occupancy, tensor_active, dram_bw. Keyed by PCI bus id so slots map
    to devices whatever the enumeration order. Calibrated on GH200 with probes/gpu_load_probe.py: linear in a registered duty cycle,
    zero on an idle card, sm_active/graphics_util 0.96 for a dense BF16 matmul. Unavailable (pre-Hopper, no NVML): returns None."""
    METRICS={1:'graphics_util',2:'sm_active',3:'sm_occupancy',5:'tensor_active',10:'dram_bw'}
    def __init__(self):
        self.nv=None
        try:
            # NVML bindings: `pip install nvidia-ml-py` (module pynvml); without them the launcher samples nvidia-smi instead
            import pynvml as nv; nv.nvmlInit(); self.nv=nv
            self.h={}; self.s1={}
            for i in range(nv.nvmlDeviceGetCount()):
                h=nv.nvmlDeviceGetHandleByIndex(i)
                if not nv.nvmlGpmQueryDeviceSupport(h).isSupportedDevice: continue
                bus=nv.nvmlDeviceGetPciInfo(h).busId
                bus=bus.decode() if isinstance(bus,bytes) else str(bus)
                self.h[bus.lower()[-12:]]=h; self.s1[bus.lower()[-12:]]=nv.nvmlGpmSampleAlloc(); nv.nvmlGpmSampleGet(h,self.s1[bus.lower()[-12:]])
            if not self.h: self.nv=None
        except Exception:
            self.nv=None
    def read(self):
        if not self.nv: return None
        nv=self.nv; out={}
        for bus,h in self.h.items():
            try:
                s2=nv.nvmlGpmSampleAlloc(); nv.nvmlGpmSampleGet(h,s2)
                m=nv.c_nvmlGpmMetricsGet_t(); m.version=nv.NVML_GPM_METRICS_GET_VERSION; m.numMetrics=len(self.METRICS); m.sample1=self.s1[bus]; m.sample2=s2
                for k,mid in enumerate(self.METRICS): m.metrics[k].metricId=mid
                nv.nvmlGpmMetricsGet(m); vals={}
                for k,(mid,name) in enumerate(self.METRICS.items()):
                    r=m.metrics[k]; vals[name]=round(r.value,2) if r.nvmlReturn==0 else None
                vals['mem_used_mib']=int(nv.nvmlDeviceGetMemoryInfo(h).used/2**20)
                nv.nvmlGpmSampleFree(self.s1[bus]); self.s1[bus]=s2; out[bus]=vals
            except Exception as e:
                out[bus]=dict(error=str(e)[:80])
        return out

def detect_mode(args):
    if args.mode!='auto': return args.mode
    if args.fake_gpus: return 'fake'
    if os.environ.get('SLURM_JOB_ID'): return 'slurm'
    return 'local'

def gpu_slots(args, mode):
    if mode=='cpu': return []
    if mode=='fake':
        return [('fake%d'%i, None) for i in range(args.fake_gpus)]
    if mode=='slurm':
        env=os.environ.get('SLURM_STEP_GPUS') or os.environ.get('SLURM_JOB_GPUS') or os.environ.get('CUDA_VISIBLE_DEVICES')
        if env:
            ids=[x.strip() for x in env.split(',') if x.strip()]
            pci={i:b for i,b in nvidia_gpus()}
            return [(i, pci.get(i)) for i in ids]
        return []   # Slurm allocated no GPUs to this job: never assume the node's cards are ours
    if args.gpus:
        pci={i:b for i,b in nvidia_gpus()}
        return [(i, pci.get(i)) for i in parse_list(args.gpus)]
    return nvidia_gpus()

def parse_list(s): return [x.strip() for x in s.split(',') if x.strip()]

def assign_cores(all_cores, slots, gpu_tasks, cpu_tasks, gpu_cores, numa, reserve=0):
    """GPU task i gets gpu_cores cores, NUMA-local to its GPU when known; CPU tasks take their thread counts from what is left.
    The lowest `reserve` cores of the allocation are kept away from GPU feeders (the OS, IRQs, the Slurm step and this launcher's
    samplers live there; on GH200 a GPU task whose feeder held core 0 ran 15-30 % slower); sidecars may use them
    as a last resort. The reservation is skipped when the allocation is too small for it (login-node tests)."""
    needed=sum((t.get('threads', gpu_cores) or gpu_cores) for t in gpu_tasks)+sum(t.get('threads',1) for t in cpu_tasks)
    reserved=list(all_cores)[:max(0,reserve)] if len(all_cores)-max(0,reserve)>=needed else []
    free=[c for c in all_cores if c not in reserved]; plan={'reserved':reserved}
    for i,t in enumerate(gpu_tasks):
        want=t.get('threads', gpu_cores) or gpu_cores
        pool=None
        if numa and slots[i][1]:
            loc=local_cpulist(slots[i][1])
            if loc: pool=[c for c in free if c in loc]
        if not pool: pool=free
        take=pool[-want:] if want<=len(pool) else pool   # feeders take the HIGHEST cores of their pool; the low cores of each processor carry the OS, IRQs, the Slurm step, this launcher and the sidecars
        if len(take)<want: raise SystemExit(f'not enough cores for GPU task {t["name"]}: want {want}, free {len(pool)}')
        plan[('gpu',i)]=take; free=[c for c in free if c not in take]
    spare=list(reserved)                      # reserved cores: sidecars only, after every free core is taken
    for j,t in enumerate(cpu_tasks):
        want=t.get('threads',1)
        take=free[:want]
        if len(take)<want and spare:
            extra=spare[:want-len(take)]; take=take+extra; spare=[c for c in spare if c not in extra]
            plan.setdefault('reserved_used_by',[]).append(t['name'])
        if len(take)<want:
            if os.environ.get('SLURM_JOB_ID'): raise SystemExit(f'not enough cores for CPU task {t["name"]}: want {want}, free {len(free)}')
            take=(free or list(all_cores))[:max(1,want)]  # local mode: oversubscribe rather than refuse
            plan[('cpu',j)]=take; plan.setdefault('oversubscribed',[]).append(t['name']); continue
        plan[('cpu',j)]=take; free=[c for c in free if c not in take]
    plan['free']=free
    return plan

def main():
    ap=argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--spec', required=True); ap.add_argument('--receipt-dir', required=True)
    ap.add_argument('--mode', default='auto', choices=['auto','slurm','local','fake','cpu'], help="cpu: no GPU slots; the spec must have no gpu_tasks")
    ap.add_argument('--gpus', default='', help='local mode: GPU indices to use, e.g. 0,1')
    ap.add_argument('--fake-gpus', type=int, default=0, help='simulate N GPU slots with no GPU visible to the tasks')
    ap.add_argument('--cores', default='', help='cores to use, e.g. 6-11,17-23 (default: this process affinity mask)')
    ap.add_argument('--gpu-cores', type=int, default=0, help='cores per GPU task (default: split evenly after CPU tasks)')
    ap.add_argument('--numa', action='store_true', help='prefer cores local to each GPU (uses sysfs local_cpulist)')
    ap.add_argument('--reserve-cores', type=int, default=2, help='lowest cores kept away from GPU feeders (OS/IRQ/launcher noise; default 2; 0 disables)')
    ap.add_argument('--allow-partial', action='store_true'); ap.add_argument('--reason', default='')
    ap.add_argument('--nice', type=int, default=0); ap.add_argument('--sample', type=float, default=0, help='nvidia-smi sampling period in seconds (0 = off)')
    ap.add_argument('--grace', type=float, default=60); ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--sidecar-nice', type=int, default=10, help='nice value for cpu_tasks and cpu_pools (GPU tasks keep --nice)')
    ap.add_argument('--cpu-sample', type=float, default=0, help='sample CPU busy fraction of GPU cores and sidecar cores every N seconds into the receipt')
    ap.add_argument('--idle-gpu-minutes', type=float, default=45.0, help='end the job when every GPU slot has read under --idle-gpu-util percent for this many minutes (needs --sample; 0 disables; default 45)')
    ap.add_argument('--idle-gpu-util', type=float, default=1.0, help='the utilisation (percent) at or above which a slot counts as active for the idle watchdog (default 1)')
    args=ap.parse_args()
    spec=json.load(open(args.spec)); gpu_tasks=spec.get('gpu_tasks',[]); cpu_tasks=spec.get('cpu_tasks',[]); pools=spec.get('cpu_pools',[])
    for pl in pools:
        pl.setdefault('threads',1); pl.setdefault('parallel',1)
        try: pl['_cmds']=[l.rstrip('\n') for l in open(os.path.join(pl.get('cwd') or os.getcwd(), pl['cmd_file'])) if l.strip() and not l.startswith('#')]
        except OSError as e: print(f"REFUSED: cpu_pool {pl.get('name')}: its command file cannot be read ({e})", file=sys.stderr); sys.exit(2)
    mode=detect_mode(args)
    if mode=='cpu' and gpu_tasks: raise SystemExit('--mode cpu with gpu_tasks in the spec; drop them or run in a GPU allocation')
    if not gpu_tasks and mode!='fake': mode='cpu'   # a CPU-only spec never claims GPU slots, in Slurm or locally
    slots=gpu_slots(args,mode)
    if not slots and gpu_tasks: raise SystemExit('no GPU slots found; use --gpus, --fake-gpus, or run inside a Slurm allocation with GPUs')
    if len(gpu_tasks)>len(slots): raise SystemExit(f'refused: {len(gpu_tasks)} GPU tasks for {len(slots)} slots')
    if len(gpu_tasks)<len(slots) and not args.allow_partial:
        print(f'REFUSED by the packing rule: {len(gpu_tasks)} GPU tasks for {len(slots)} GPU slots; add tasks or pass --allow-partial --reason "..."', file=sys.stderr); sys.exit(2)
    if args.allow_partial and not args.reason: raise SystemExit('--allow-partial needs --reason')
    cores=parse_cores(args.cores) if args.cores else sorted(os.sched_getaffinity(0))
    cpu_threads=sum(t.get('threads',1) for t in cpu_tasks)
    gpu_cores=args.gpu_cores or max(1,(len(cores)-cpu_threads-max(0,args.reserve_cores))//max(1,len(gpu_tasks))) if gpu_tasks else 0
    plan=assign_cores(cores, slots, gpu_tasks, cpu_tasks, gpu_cores, args.numa, args.reserve_cores)
    free=list(plan['free'])
    for k,pl in enumerate(pools):
        want=pl['parallel']*pl['threads']
        if len(free)<want:
            if os.environ.get('SLURM_JOB_ID') and len(free)==0: raise SystemExit(f"no free cores for cpu_pool {pl['name']}")
            pl['parallel']=max(1,len(free)//pl['threads']) if free else 1; want=pl['parallel']*pl['threads']
        pl['_cores']=free[:want] if free else list(cores)[:want]; free=[c for c in free if c not in pl['_cores']]
    plan['free']=free
    os.makedirs(args.receipt_dir, exist_ok=True)
    receipt=dict(job=spec.get('job',os.path.basename(args.spec)), host=socket.gethostname(), mode=mode, started=now(),
                 slurm={k:v for k,v in os.environ.items() if k.startswith('SLURM_') and k in ('SLURM_JOB_ID','SLURM_JOB_NAME','SLURM_JOB_NODELIST','SLURM_JOB_GPUS','SLURM_ARRAY_TASK_ID','SLURM_JOB_PARTITION','SLURM_JOB_ACCOUNT')},
                 gpu_slots=[s[0] for s in slots], cores=cores, gpu_cores_per_task=gpu_cores, numa=args.numa,
                 packing_rule=dict(slots=len(slots), gpu_tasks=len(gpu_tasks), cpu_tasks=len(cpu_tasks), allow_partial=args.allow_partial, reason=args.reason),
                 oversubscribed=plan.get('oversubscribed',[]), free_cores=plan['free'], reserved_cores=plan.get('reserved',[]), reserved_used_by=plan.get('reserved_used_by',[]), tasks=[], gpu_samples=[], interrupted=None)
    launches=[]
    for kind,tasks in (('gpu',gpu_tasks),('cpu',cpu_tasks)):
        for i,t in enumerate(tasks):
            c=plan[(kind,i)]; env=dict(os.environ); env.update({k:str(v) for k,v in t.get('env',{}).items()})
            slot = slots[i][0] if kind=='gpu' else ''
            env['CUDA_VISIBLE_DEVICES'] = ('' if mode=='fake' else slot) if kind=='gpu' else ''
            env.update(PACK_SLOT=str(slot), PACK_TASK=t['name'], PACK_JOB=receipt['job'], PACK_CORES=','.join(map(str,c)), PACK_MODE=mode, PACK_RECEIPT_DIR=os.path.abspath(args.receipt_dir))
            th=str(t.get('threads', gpu_cores if kind=='gpu' else 1) or 1)
            env.update(OMP_NUM_THREADS=th, MKL_NUM_THREADS=th, OPENBLAS_NUM_THREADS=th, NUMEXPR_NUM_THREADS=th, TORCH_NUM_THREADS=th, PYTHONDONTWRITEBYTECODE=env.get('PYTHONDONTWRITEBYTECODE','1'))
            cmd=['taskset','-c',','.join(map(str,c))]
            nv = args.nice if kind=='gpu' else max(args.nice, args.sidecar_nice)
            if nv: cmd+=['nice','-n',str(nv)]
            pre = (f"ulimit -v {int(float(t['mem_gb'])*1048576)}; " if t.get('mem_gb') else '')
            cmd+=['bash','-lc',TASK_SHELL+pre+t['cmd']]
            launches.append(dict(kind=kind,name=t['name'],slot=slot,cores=c,cmd=t['cmd'],cwd=t.get('cwd') or os.getcwd(),env=env,argv=cmd))
    for k,pl in enumerate(pools):
        # one worker per parallel slot, each pinned to its own cores, consuming the shared command list by index (stride = parallel)
        for w in range(pl['parallel']):
            c=pl['_cores'][w*pl['threads']:(w+1)*pl['threads']] or pl['_cores'][:1]
            env=dict(os.environ); env.update({k2:str(v) for k2,v in pl.get('env',{}).items()})
            th=str(pl['threads']); env.update(CUDA_VISIBLE_DEVICES='',PACK_SLOT='',PACK_TASK=f"{pl['name']}-w{w}",PACK_JOB=receipt['job'],PACK_CORES=','.join(map(str,c)),PACK_MODE=mode,
                                        OMP_NUM_THREADS=th,MKL_NUM_THREADS=th,OPENBLAS_NUM_THREADS=th,NUMEXPR_NUM_THREADS=th,TORCH_NUM_THREADS=th,PYTHONDONTWRITEBYTECODE='1')
            rd=os.path.abspath(args.receipt_dir)
            cmdf=os.path.join(rd,f"{pl['name']}.cmds")
            if w==0:
                with open(cmdf,'w') as io_cmds: io_cmds.write('\n'.join(pl['_cmds'])+'\n')
                with open(os.path.join(rd,'pool_worker.py'),'w') as pw: pw.write(POOL_WORKER)
            logf=os.path.join(rd,f"{pl['name']}-w{w}.pool.log")
            pre=(f"ulimit -v {int(float(pl['mem_gb'])*1048576)}; " if pl.get('mem_gb') else '')
            script=pre+f"exec {sys.executable} '{os.path.join(rd,'pool_worker.py')}' '{cmdf}' {w} {pl['parallel']} '{logf}'"
            cmd=['taskset','-c',','.join(map(str,c)),'nice','-n',str(max(args.nice,args.sidecar_nice)),'bash','-lc',script]
            launches.append(dict(kind='pool',name=f"{pl['name']}-w{w}",slot='',cores=c,cmd=f"pool {pl['name']} worker {w} of {pl['parallel']} over {len(pl['_cmds'])} commands",cwd=pl.get('cwd') or os.getcwd(),env=env,argv=cmd))
    missing=[f"{l['name']} ({l['cwd']})" for l in launches if not os.path.isdir(l['cwd'])]
    if missing:   # every task's folder is checked before anything starts (a later task's missing folder used to leave the earlier ones running, with no receipt)
        print('REFUSED: task folder(s) missing: ' + '; '.join(missing), file=sys.stderr); sys.exit(2)
    if args.dry_run:
        for l in launches: print(f"[{l['kind']}] {l['name']}: slot={l['slot']!r} cores={l['cores']} cwd={l['cwd']} cmd={l['cmd']}")
        print('free cores:',plan['free']); return
    procs={}; receipt['launch_error']=None
    for k,l in enumerate(launches):
        try:
            if os.environ.get('PACK_TEST_FAIL_LAUNCH')==l['name']: raise OSError('a launch failure made by PACK_TEST_FAIL_LAUNCH (tests only)')
            out=open(os.path.join(args.receipt_dir,f"{l['name']}.out"),'wb'); err=open(os.path.join(args.receipt_dir,f"{l['name']}.err"),'wb')
            l['start']=time.time(); l['start_iso']=now()
            p=subprocess.Popen(l['argv'],cwd=l['cwd'],env=l['env'],stdout=out,stderr=err,start_new_session=True)
        except Exception as e:
            # a task that cannot be started ends the job cleanly: the tasks already started are stopped and reaped below, and the receipt
            # records it (this used to end in a traceback, with the started tasks still running and no receipt)
            receipt['launch_error']=dict(task=l['name'],error=f"{type(e).__name__}: {e}"[:300],at=now(),not_started=[x['name'] for x in launches[k:]])
            print(f"{now()} LAUNCH FAILED: task {l['name']}: {receipt['launch_error']['error']}; stopping the {len(procs)} task(s) already started; the receipt records it",file=sys.stderr,flush=True)
            for pid in procs:
                try: os.killpg(pid, signal.SIGTERM)
                except ProcessLookupError: pass
            t_end=time.time()+min(args.grace,10)
            while time.time()<t_end and any(p2.poll() is None for p2,_ in procs.values()): time.sleep(0.1)
            for pid in procs:
                try: os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError: pass
            break
        l['pid']=p.pid; procs[p.pid]=(p,l)
    stop=threading.Event()
    def forward(sig, frame):
        receipt['interrupted']=dict(signal=int(sig),at=now())
        for pid,(p,l) in procs.items():
            if p.poll() is None:
                try: os.killpg(pid, signal.SIGUSR1 if sig==signal.SIGUSR1 else signal.SIGTERM)
                except ProcessLookupError: pass
        if sig!=signal.SIGUSR1:
            time.sleep(args.grace)
            for pid,(p,l) in procs.items():
                p.poll()   # reaps a task shell that has ended; whatever it started may still be in its group
                try: os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError): pass
    signal.signal(signal.SIGUSR1, forward); signal.signal(signal.SIGTERM, forward)
    gpm=GpmSampler() if (args.sample and mode!='fake') else None
    slot_bus={s[0]:(s[1] or '').lower()[-12:] for s in slots}
    receipt['gpu_sampler']=('gpm' if (gpm and gpm.nv) else 'nvidia-smi'); receipt['gpm_samples']=[]
    fake_util=os.environ.get('PACK_FAKE_GPU_UTIL')   # tests only (fake mode): every slot samples this utilisation in percent
    wd=dict(minutes=args.idle_gpu_minutes, util_percent=args.idle_gpu_util, triggered=None, unread_samples=0, last_active_by_slot={str(s[0]):'start' for s in slots}); receipt['gpu_idle_watchdog']=wd
    slot_ids={str(s[0]) for s in slots}; last_active=[time.time()]; last_active_iso=['start']
    def watchdog(utils, tnow):
        """utils: {slot: utilisation percent, or None for a slot that could not be read} for every slot of one sample, or None when
        nothing could be read. A sample with any slot not read counts as active, never as idle: the slots that were read used to stand
        for all of them, so a job could be ended while one card was never read."""
        t_iso=now()
        for slot,u in (utils or {}).items():
            if u is not None and u>=wd['util_percent']: wd['last_active_by_slot'][slot]=t_iso
        if utils is None or set(utils)!=slot_ids or any(u is None for u in utils.values()):
            wd['unread_samples']+=1; last_active[0]=tnow; last_active_iso[0]=t_iso; return
        if any(u>=wd['util_percent'] for u in utils.values()): last_active[0]=tnow; last_active_iso[0]=t_iso; return
        if wd['minutes']>0 and wd['triggered'] is None and tnow-last_active[0]>=wd['minutes']*60:
            wd.update(triggered=t_iso, idle_since=last_active_iso[0], idle_s=round(tnow-last_active[0],1), action='SIGTERM to every task: every GPU slot under the threshold for the whole window')
            print(f"{t_iso} GPU-IDLE WATCHDOG: every GPU slot under {wd['util_percent']} % utilisation for {wd['minutes']} min (idle since {last_active_iso[0]}, {wd['idle_s']} s; last active by slot {wd['last_active_by_slot']}); ending the job with SIGTERM to every task; the receipt records it", flush=True)
            os.kill(os.getpid(), signal.SIGTERM)
    def sampler():
        while not stop.wait(args.sample):
            utils=None; tnow=time.time()
            if fake_util is not None:   # tests only: one value for every slot, or one per slot, x for a slot that cannot be read
                vals=[x.strip() for x in fake_util.split(',')]; vals=vals*len(slots) if len(vals)==1 else vals+['x']*len(slots)
                fv={}
                for (s,_),x in zip(slots,vals):
                    try: fv[str(s)]=float(x)
                    except ValueError: pass
                utils=slot_readings(slot_ids,fv,{})
                receipt['gpu_samples'].append(dict(t=now(),rows=[f"{s[0]}, {int(utils[str(s[0])]) if utils[str(s[0])] is not None else '[N/A]'}, 0" for s in slots]))
            else:
                gv={}; sv={}
                if gpm and gpm.nv:
                    r=gpm.read(); t=now()
                    if r is not None:
                        g={slot:r.get(bus) for slot,bus in slot_bus.items() if bus in r}; receipt['gpm_samples'].append(dict(t=t,gpus=g))
                        gv={str(slot):x.get('graphics_util') for slot,x in g.items() if isinstance(x,dict) and 'error' not in x}
                try:
                    r=subprocess.run(['nvidia-smi','--query-gpu=index,utilization.gpu,memory.used','--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=10)
                    rows=[x.strip() for x in r.stdout.strip().splitlines()]; receipt['gpu_samples'].append(dict(t=now(),rows=rows))
                    for row in rows:
                        p=[y.strip() for y in row.split(',')]
                        if len(p)>=2 and p[0] in slot_ids and p[1].isdigit(): sv[p[0]]=float(p[1])
                except Exception as e: receipt['gpu_samples'].append(dict(t=now(),error=str(e)))
                utils=slot_readings(slot_ids,gv,sv)   # every slot: GPM's reading, else nvidia-smi's for that slot, else None (not read)
                if all(u is None for u in utils.values()): utils=None
            watchdog(utils, tnow)
    if args.sample and (mode!='fake' or fake_util is not None) and not receipt['launch_error']: threading.Thread(target=sampler,daemon=True).start()
    gpu_cores=sorted({c for l in launches if l['kind']=='gpu' for c in l['cores']}); side_cores=sorted({c for l in launches if l['kind']!='gpu' for c in l['cores']})
    receipt['cpu_samples']=[]; receipt['gpu_cores']=gpu_cores; receipt['sidecar_cores']=side_cores
    def readstat():
        d={}
        for line in open('/proc/stat'):
            if line.startswith('cpu') and line[3:4].isdigit():
                f=line.split(); n=int(f[0][3:]); v=list(map(int,f[1:])); idle=v[3]+v[4]; d[n]=(sum(v),idle)
        return d
    def cpu_sampler():
        prev=readstat()
        while not stop.wait(args.cpu_sample):
            cur=readstat()
            def busy(cs):
                tot=sum(cur[c][0]-prev[c][0] for c in cs if c in cur and c in prev); idl=sum(cur[c][1]-prev[c][1] for c in cs if c in cur and c in prev)
                return round(1-idl/tot,3) if tot else None
            receipt['cpu_samples'].append(dict(t=now(),gpu_cores_busy=busy(gpu_cores),sidecar_cores_busy=busy(side_cores),other_cores_busy=busy([c for c in cur if c not in gpu_cores and c not in side_cores])))
            prev=cur
    if args.cpu_sample and not receipt['launch_error']: threading.Thread(target=cpu_sampler,daemon=True).start()
    t0=time.time(); pending=dict(procs)
    while pending:
        try: pid,status,ru=os.wait4(-1,0)
        except ChildProcessError: break
        if pid not in pending: continue
        p,l=pending.pop(pid); p.returncode = os.waitstatus_to_exitcode(status) if hasattr(os,'waitstatus_to_exitcode') else status
        receipt['tasks'].append(dict(kind=l['kind'],name=l['name'],slot=l['slot'],cores=l['cores'],cwd=l['cwd'],cmd=l['cmd'],pid=pid,
            start=l['start_iso'],end=now(),wall_s=round(time.time()-l['start'],3),exit=p.returncode,utime_s=round(ru.ru_utime,3),stime_s=round(ru.ru_stime,3),maxrss_kb=ru.ru_maxrss,
            stdout=f"{l['name']}.out",stderr=f"{l['name']}.err"))
    # Children reaped inside the signal handler (its p.poll() calls, after SIGTERM and the grace kill) never reach wait4 above;
    # record them from the Popen return code so a cancelled or killed job still has one row per task (no rusage for those).
    for pid,(p,l) in list(pending.items()):
        rc=p.poll()
        if rc is None: continue
        pending.pop(pid)
        receipt['tasks'].append(dict(kind=l['kind'],name=l['name'],slot=l['slot'],cores=l['cores'],cwd=l['cwd'],cmd=l['cmd'],pid=pid,
            start=l['start_iso'],end=now(),wall_s=round(time.time()-l['start'],3),exit=rc,utime_s=0.0,stime_s=0.0,maxrss_kb=0,
            stdout=f"{l['name']}.out",stderr=f"{l['name']}.err",reaped_in_signal_handler=True))
    stop.set(); wall=time.time()-t0
    usr1=receipt['interrupted'] is not None and receipt['interrupted'].get('signal')==int(signal.SIGUSR1)
    receipt['pools']=[]
    for pl in pools:
        # a worker's log: 'index status seconds' per command it ran, and ' interrupted' when the checkpoint signal or SIGTERM came while
        # the command ran. The commands not finished, the interrupted ones and those never started, go to the pool's resume
        # file in file order, for the next segment; an interrupted command's status is recorded, not judged.
        done=0; failed=0; secs=0.0; cut={}; seen=set()
        for w in range(pl['parallel']):
            lf=os.path.join(args.receipt_dir,f"{pl['name']}-w{w}.pool.log")
            if os.path.exists(lf):
                for line in open(lf):
                    parts=line.split()
                    if len(parts)>=3 and parts[0].isdigit():
                        i=int(parts[0]); seen.add(i); secs+=float(parts[2])
                        if parts[3:4]==['interrupted']: cut[i]=int(parts[1]) if parts[1].lstrip('-').isdigit() else parts[1]
                        else: done+=1; failed+= (parts[1]!='0')
        left=[i for i in range(len(pl['_cmds'])) if i not in seen]
        resume=sorted(set(cut)|set(left)); rf=None
        if resume:
            rf=f"{pl['name']}.resume.cmds"
            with open(os.path.join(args.receipt_dir,rf),'w') as fh: fh.write(''.join(pl['_cmds'][i]+'\n' for i in resume))
        receipt['pools'].append(dict(name=pl['name'],commands=len(pl['_cmds']),parallel=pl['parallel'],threads=pl['threads'],cores=pl['_cores'],completed=done,failed=failed,
                                     interrupted=len(cut),interrupted_exits={str(i):cut[i] for i in sorted(cut)},not_started=len(left),resume_file=rf,command_seconds=round(secs,3)))
    # A SIGUSR1 interruption after which every task exited 0 is the contract working (checkpoint, exit 0, resume next segment),
    # not a failure: the receipt says checkpoint_segment=True and all_ok stays True. SIGTERM, or any non-zero exit, is a failure.
    # A pool under SIGUSR1 counts when no command it finished had failed: the rest is in its resume file.
    exits_ok=all(t['exit']==0 for t in receipt['tasks']) and all(p['failed']==0 and (p['completed']==p['commands'] or usr1) for p in receipt['pools'])
    receipt.update(ended=now(),wall_s=round(wall,3),gpu_slot_hours_held=round(len(slots)*wall/3600,6),cpu_seconds=round(sum(t['utime_s']+t['stime_s'] for t in receipt['tasks']),3),
                   checkpoint_segment=bool(usr1 and exits_ok and not receipt['launch_error']), all_ok=exits_ok and (receipt['interrupted'] is None or usr1) and not receipt['launch_error'])
    # per-GPU activity summary: interval-averaged GPM where available (the truthful one), else nvidia-smi's instantaneous samples
    gsum={}
    for slot in [s[0] for s in slots]:
        vals=[x['gpus'][slot] for x in receipt.get('gpm_samples',[]) if x.get('gpus',{}).get(slot) and 'error' not in x['gpus'][slot]]
        if vals:
            def mean(k): v=[x[k] for x in vals if x.get(k) is not None]; return round(sum(v)/len(v),1) if v else None
            gsum[slot]=dict(source='gpm',samples=len(vals),gpu_util=mean('graphics_util'),sm_active=mean('sm_active'),sm_occupancy=mean('sm_occupancy'),tensor_active=mean('tensor_active'),dram_bw=mean('dram_bw'),
                            busy_fraction=round(sum(1 for x in vals if (x.get('graphics_util') or 0)>=5)/len(vals),2),peak_mem_mib=max((x.get('mem_used_mib') or 0) for x in vals))
        else:
            u=[]; mem=0
            for smp in receipt.get('gpu_samples',[]):
                for row in smp.get('rows',[]):
                    p=[y.strip() for y in row.split(',')]
                    if len(p)>=3 and p[0]==str(slot) and p[1].isdigit(): u.append(int(p[1])); mem=max(mem,int(p[2]) if p[2].isdigit() else 0)
            if u: gsum[slot]=dict(source='nvidia-smi',samples=len(u),gpu_util=round(sum(u)/len(u),1),sm_active=None,busy_fraction=round(sum(1 for x in u if x>=5)/len(u),2),peak_mem_mib=mem)
    if gsum:
        bf=[g['busy_fraction'] for g in gsum.values()]
        receipt['gpu_summary']=gsum; receipt['gpu_imbalance']=bool(len(bf)>1 and max(bf)-min(bf)>=0.5)
    json.dump(receipt,open(os.path.join(args.receipt_dir,'receipt.json'),'w'),indent=1)
    with open(os.path.join(args.receipt_dir,'receipt.md'),'w') as f:
        f.write(f"# {receipt['job']} on {receipt['host']} ({mode})\n\nslots {len(slots)}, gpu tasks {len(gpu_tasks)}, cpu tasks {len(cpu_tasks)}, wall {receipt['wall_s']} s, GPU-slot-hours held {receipt['gpu_slot_hours_held']}, cpu seconds {receipt['cpu_seconds']}, all ok {receipt['all_ok']}\n\n")
        f.write('| task | kind | slot | cores | wall s | exit | cpu s | max RSS MB |\n|---|---|---|---|---:|---:|---:|---:|\n')
        for t in sorted(receipt['tasks'],key=lambda x:(x['kind'],x['name'])):
            f.write(f"| {t['name']} | {t['kind']} | {t['slot']} | {','.join(map(str,t['cores']))} | {t['wall_s']} | {t['exit']} | {round(t['utime_s']+t['stime_s'],1)} | {round(t['maxrss_kb']/1024,1)} |\n")
        for p in receipt['pools']: f.write(f"\npool {p['name']}: {p['completed']}/{p['commands']} commands done, {p['failed']} failed, {p['parallel']} workers x {p['threads']} threads on cores {','.join(map(str,p['cores']))}, {p['command_seconds']} command-seconds" + (f"; {p['interrupted']} interrupted (exits {p['interrupted_exits']}) and {p['not_started']} not started, kept for the next segment in {p['resume_file']}" if p.get('resume_file') else '') + "\n")
        if receipt.get('gpu_summary'):
            f.write(f"\nGPU activity per slot ({receipt['gpu_sampler']}, means over the job; gpu_util = share of time a kernel ran, sm_active = share of SMs busy (Active SM), busy = share of samples with gpu_util >= 5%):\n\n| slot | samples | gpu_util % | sm_active % | occupancy % | tensor % | dram % | busy | peak mem MiB |\n|---|---:|---:|---:|---:|---:|---:|---:|---:|\n")
            for slot,g in sorted(receipt['gpu_summary'].items()):
                f.write(f"| {slot} | {g['samples']} | {g.get('gpu_util')} | {g.get('sm_active') if g.get('sm_active') is not None else '-'} | {g.get('sm_occupancy','-')} | {g.get('tensor_active','-')} | {g.get('dram_bw','-')} | {g['busy_fraction']} | {g['peak_mem_mib']} |\n")
            if receipt.get('gpu_imbalance'): f.write("\nGPU IMBALANCE: the busy fractions of the slots differ by 0.5 or more (one card mostly idle while another works).\n")
        if receipt['cpu_samples']:
            g=[x['gpu_cores_busy'] for x in receipt['cpu_samples'] if x['gpu_cores_busy'] is not None]; sd=[x['sidecar_cores_busy'] for x in receipt['cpu_samples'] if x['sidecar_cores_busy'] is not None]
            f.write(f"\ncpu busy (mean over samples): gpu cores {round(sum(g)/len(g),3) if g else 'n/a'}, sidecar cores {round(sum(sd)/len(sd),3) if sd else 'n/a'}, {len(receipt['cpu_samples'])} samples\n")
        if receipt.get('gpu_idle_watchdog',{}).get('triggered'): f.write(f"\nGPU-IDLE WATCHDOG: every GPU slot under {receipt['gpu_idle_watchdog']['util_percent']} % utilisation for {receipt['gpu_idle_watchdog']['minutes']} min; the launcher ended the job at {receipt['gpu_idle_watchdog']['triggered']} (idle since {receipt['gpu_idle_watchdog'].get('idle_since')}, {receipt['gpu_idle_watchdog']['idle_s']} s; last active by slot {receipt['gpu_idle_watchdog']['last_active_by_slot']}).\n")
        if receipt['interrupted']: f.write(f"\ninterrupted: {receipt['interrupted']}" + (" (SIGUSR1 checkpoint segment: every task exited 0; resume the next segment)" if receipt.get('checkpoint_segment') else " (not a clean checkpoint)") + "\n")
        if receipt.get('launch_error'): f.write(f"\nLAUNCH FAILED: task {receipt['launch_error']['task']}: {receipt['launch_error']['error']}; not started: {', '.join(receipt['launch_error']['not_started'])}\n")
    print(json.dumps(dict(job=receipt['job'],mode=mode,wall_s=receipt['wall_s'],gpu_slot_hours_held=receipt['gpu_slot_hours_held'],all_ok=receipt['all_ok'],receipt=os.path.join(args.receipt_dir,'receipt.json'))))
    sys.exit(0 if receipt['all_ok'] else 1)

if __name__=='__main__': main()
