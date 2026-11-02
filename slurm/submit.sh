#!/bin/bash
# submit.sh: the only way a fleet job reaches Slurm. One whole node per (array) task; account, partition, gres, cores, time and the
# checkpoint warning come from the site file (slurm/site.<SITE>.env).
#
#   submit.sh [--site NAME] [--devel] [--time HH:MM:SS] [--array 0-N%M --spec-dir DIR] [--name NAME] [--pack "extra pack_node args"]
#             [--cpu-only [--cores N]] [--force-node --reason "..."] [--after ID[:ID..]] [--afterany ID[:ID..]]
#             [--low] [--hold] [--over-cap-ok] [--nodes N] [--comment "KEY=VALUE;KEY=VALUE"] [--allow-same-name] SPEC
#
# Guards. Each fails closed: if the queue or the accounting cannot be read, nothing is submitted. There is no loop and no retry here.
#   budget        the job name starts with a lane tag (alpha-...) that has a cap in caps.json; a job whose worst case (time limit x
#                 GPUs x array tasks x nodes) would take the lane over its cap is refused (exit 3; --over-cap-ok overrides), and so is
#                 every job while caps.json is missing, unreadable or empty or has no finite, non-negative cap for the lane. The check
#                 runs inside the fleet lock, and each accepted job's worst case is reserved (FLEET_RESERVATIONS_FILE) until the queue
#                 or accounting shows it, so two submissions can never spend the same headroom
#   review hold   a worst case above REVIEW_THRESHOLD_HOURS is submitted held for a human's review; the principal releases or cancels it
#   duplicates    a job whose name is already pending or running on the account is refused (exit 5) unless --allow-same-name
#   rate          more than MAX_SUBMITS_PER_10MIN submissions from the account inside ten minutes are refused (exit 6)
#   arrays        an array must carry a throttle (0-29%1), and throttle x nodes may not exceed MAX_NODES_IN_FLIGHT (exit 2 / 4)
#   no requeue    jobs are submitted --no-requeue, so a node failure never restarts a job by itself
#   ceiling       where MAX_NODES_IN_FLIGHT is set, a job that would put more of the account's nodes in flight (running, plus pending
#                 without a dependency and not held, the pending part of an array by the elements its throttle lets run at once;
#                 slurm/inflight.py) is ACCEPTED AND HELD, written to the fleet queue (FLEET_QUEUE_FILE) and released first in first
#                 out by slurm/fleet_dispatch.sh, which runs at the end of every fleet job and at every submission.
#                 Submit once and never retry. Held (--hold) and chained submissions are not counted.
#   one at a time submissions take a fleet-wide lock (an atomic mkdir on the shared filesystem), always; its holder refreshes it while
#                 it works, and a lock nobody has refreshed for FLEET_LOCK_STALE_S seconds (180) belongs to a dead holder and is taken over
# --nodes N: a multi-node job (N whole nodes); the same spec runs on every node under srun, one launcher per node, with SLURM_NNODES,
#           SLURM_NODEID and SLURM_JOB_NODELIST available to the tasks for rendezvous (NCCL). Charged N x GPUS_PER_NODE an hour.
# --comment: passed to sbatch --comment (site job overrides; on JSC systems the documented keys include CUDA_MPS=1,
#           NVIDIA_GPU_CLOCKS, CPU_POWER_CAP and THP). Commas and quotes are refused. Recorded in the job log.
# --low: filler priority (sbatch --nice=1000). --hold: submitted held, released with `scontrol release ID`.
# --after / --afterany: chain behind earlier jobs (Slurm afterok / afterany); a chain that can never start is cancelled, not left pending.
# CPU-only specs (no gpu_tasks): allowed where the site has CPU_PARTITION (shared cores, no gres, --cores N); elsewhere refused unless
# --force-node --reason, which holds (and is charged for) a whole node.
# Exit codes: 0 submitted or queued; 1 usage or a missing spec; 2 refused spec or options; 3 budget; 4 ceiling, lock or unreadable queue;
# 5 duplicate name; 6 rate guard; anything else is sbatch's own failure.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; JDIR="$(cd "$HERE/.." && pwd)"
SITE=local; DEVEL=0; TIME=""; ARRAY=""; SPEC_DIR=""; NAME=""; PACK_ARGS=""; CPU_ONLY=0; CORES=""; FORCE_NODE=0; REASON=""; AFTER=""; AFTERANY=""; LOW=0; HOLD=0; OVERCAP=0; NODES=1; COMMENT=""; ALLOW_SAME=0
while [ $# -gt 0 ]; do case "$1" in
  --site) SITE="$2"; shift 2;; --devel) DEVEL=1; shift;; --time) TIME="$2"; shift 2;; --array) ARRAY="$2"; shift 2;;
  --spec-dir) SPEC_DIR="$2"; shift 2;; --name) NAME="$2"; shift 2;; --pack) PACK_ARGS="$2"; shift 2;;
  --cpu-only) CPU_ONLY=1; shift;; --cores) CORES="$2"; shift 2;; --after) AFTER="$2"; shift 2;; --afterany) AFTERANY="$2"; shift 2;; --low) LOW=1; shift;; --hold) HOLD=1; shift;; --over-cap-ok) OVERCAP=1; shift;; --nodes) NODES="$2"; shift 2;; --comment) COMMENT="$2"; shift 2;; --allow-same-name) ALLOW_SAME=1; shift;; --force-node) FORCE_NODE=1; shift;; --reason) REASON="$2"; shift 2;; *) SPEC="$1"; shift;; esac; done
. "$HERE/site.$SITE.env"
if [ -n "$ARRAY" ]; then [ -n "$SPEC_DIR" ] || { echo "--array needs --spec-dir"; exit 1; }; SPEC="${SPEC_DIR}/node_0.json"
  case "$ARRAY" in *%*) THROTTLE="${ARRAY##*%}";; *) echo "refused: an array needs a throttle, for example --array 0-29%1 (one node at a time); without one every task may start at once" >&2; exit 2;; esac
  case "$THROTTLE" in ''|*[!0-9]*|0) echo "refused: array throttle '$THROTTLE' is not a positive number" >&2; exit 2;; esac
fi
[ -f "${SPEC:?spec path required}" ] || { echo "spec not found: $SPEC"; exit 1; }
# The batch script runs in the tooling folder (--chdir), so a spec or spec directory given relative to the caller's folder must be made
# absolute here (a relative path once killed a job at its start).
SPEC="$(cd "$(dirname "$SPEC")" && pwd)/$(basename "$SPEC")"
[ -n "$SPEC_DIR" ] && SPEC_DIR="$(cd "$SPEC_DIR" && pwd)"
NAME="${NAME:-$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('job','node'))" "$SPEC")}"
NGPU_TASKS=$(python3 -c "import json,sys;print(len(json.load(open(sys.argv[1])).get('gpu_tasks',[])))" "$SPEC")
SPEC_THREADS=$(python3 -c "import json,sys;s=json.load(open(sys.argv[1]));print(sum(t.get('threads',1) for t in s.get('cpu_tasks',[])))" "$SPEC")
if [ "$NGPU_TASKS" = 0 ] && [ $CPU_ONLY = 0 ] && [ $FORCE_NODE = 0 ]; then echo "refused: the spec has no gpu_tasks. Use --cpu-only (shared cores, where the site allows it) or --force-node --reason (a whole node, charged as such)."; exit 2; fi
if [ $CPU_ONLY = 1 ] && [ "$NGPU_TASKS" != 0 ]; then echo "refused: --cpu-only with gpu_tasks in the spec"; exit 2; fi
if [ $FORCE_NODE = 1 ] && [ -z "$REASON" ]; then echo "--force-node needs --reason"; exit 2; fi
if [ $DEVEL = 1 ]; then PART="$DEVEL_PARTITION"; TIME="${TIME:-$DEVEL_MAX_TIME}"; else PART="$PARTITION"; TIME="${TIME:-$MAX_TIME}"; fi
mkdir -p "$JDIR/logs" "$JDIR/receipts"
if [ $CPU_ONLY = 1 ] && [ $FORCE_NODE = 0 ]; then
  [ -n "${CPU_PARTITION:-}" ] || { echo "refused: site '$SITE' has no CPU partition; CPU work rides as sidecars in a GPU spec, or --force-node --reason holds a whole node"; exit 2; }
  CORES="${CORES:-$SPEC_THREADS}"; [ "$CORES" -ge 1 ] || CORES=1
  ARGS=(--job-name="$NAME" --account="$ACCOUNT" --partition="$CPU_PARTITION" --cpus-per-task="$CORES" --time="$TIME"
        --signal="B:USR1@$SIGNAL_BEFORE_END" --chdir="$JDIR" --no-requeue
        --export="ALL,FLEET_DIR=$JDIR,SPEC=$SPEC,SPEC_DIR=$SPEC_DIR,ENV_ACTIVATE=$ENV_ACTIVATE,PACK_EXTRA=$PACK_EXTRA,PACK_ARGS=$PACK_ARGS --mode cpu,CPU_ONLY=1,FLEET_SBATCH_COMMENT=$COMMENT,FLEET_SITE=$SITE")
else
  ARGS=(--job-name="$NAME" --account="$ACCOUNT" --partition="$PART" --nodes="$NODES" --ntasks-per-node=1 --gres="gpu:$GPUS_PER_NODE" --cpus-per-task="$CPUS_PER_NODE" --time="$TIME"
        --signal="B:USR1@$SIGNAL_BEFORE_END" --chdir="$JDIR" --no-requeue
        --export="ALL,FLEET_DIR=$JDIR,SPEC=$SPEC,SPEC_DIR=$SPEC_DIR,ENV_ACTIVATE=$ENV_ACTIVATE,PACK_EXTRA=$PACK_EXTRA,PACK_ARGS=$PACK_ARGS,FORCE_NODE_REASON=$REASON,MULTI_NODES=$NODES,FLEET_SBATCH_COMMENT=$COMMENT,FLEET_SITE=$SITE")
  [ "$NODES" -gt 1 ] && echo "NOTE: multi-node job, $NODES nodes x $GPUS_PER_NODE GPUs, charged $((NODES*GPUS_PER_NODE)) GPU-hours per hour" >&2
  if [ $FORCE_NODE = 1 ]; then echo "NOTE: whole node held by a CPU-only spec (reason: $REASON); charged as $GPUS_PER_NODE GPUs per hour"; fi
fi
[ -n "$ARRAY" ] && ARGS+=(--array="$ARRAY")
[ $LOW = 1 ] && ARGS+=(--nice=1000)
[ $HOLD = 1 ] && ARGS+=(--hold)
# worst case for the budget: time limit x GPUs (0 for --cpu-only), x array size
LANE="${NAME%%-*}"; NTASKS=1; [ -n "$ARRAY" ] && NTASKS=$(python3 -c "import re,sys;s=sys.argv[1].split('%')[0];n=0
for p in s.split(','):
    a,_,b=p.partition('-'); n+= (int(b)-int(a)+1) if b else 1
print(n)" "$ARRAY")
[ -n "$LANE" ] || { echo "refused: the job name '$NAME' has no lane tag; it must start with the lane's tag and a hyphen (alpha-what-v1)" >&2; exit 2; }
if [ $CPU_ONLY = 1 ] && [ $FORCE_NODE = 0 ]; then EST=0; else EST=$(python3 -c "import sys;h,m,s=(sys.argv[1].split(':')+['0','0'])[:3];print(round((int(h)+int(m)/60+int(s)/3600)*int(sys.argv[2])*int(sys.argv[3])*int(sys.argv[4]),3))" "$TIME" "$GPUS_PER_NODE" "$NTASKS" "$NODES"); fi
if [ "${REVIEW_THRESHOLD_HOURS:-0}" != 0 ] && [ $HOLD = 0 ] && python3 -c "import sys;sys.exit(0 if float(sys.argv[1])>float(sys.argv[2]) else 1)" "$EST" "$REVIEW_THRESHOLD_HOURS"; then
  HOLD=1; ARGS+=(--hold); echo "HELD FOR REVIEW: worst case ${EST} GPU-h exceeds the review threshold ${REVIEW_THRESHOLD_HOURS}; a reviewer checks the job against the lane's plan and reports; the principal releases it (scontrol release ID) or cancels it" >&2
fi
if [ -n "$COMMENT" ]; then
  case "$COMMENT" in *,*|*\"*|*\'*) echo "refused: --comment may not contain commas or quotes; use KEY=VALUE;KEY=VALUE" >&2; exit 2;; esac
  ARGS+=(--comment="$COMMENT"); echo "NOTE: site job overrides via --comment: $COMMENT (recorded in the job log)" >&2
fi
# Guards against one node too many, a repeated job and a runaway loop (see the header). One queue snapshot feeds all of them.
# Fail closed: if the queue cannot be read or the fleet lock cannot be taken, nothing is submitted.
GUARD_QUEUED=0; WAITING=0; WANT="$NODES"
QFILE="${FLEET_QUEUE_FILE:-$JDIR/receipts/FLEET_QUEUE.log}"; LOCK="${FLEET_LOCK_DIR:-$JDIR/receipts/.fleet.lock}"
RFILE="${FLEET_RESERVATIONS_FILE:-$JDIR/receipts/FLEET_RESERVATIONS.log}"; LOCKED=0
if command -v squeue >/dev/null 2>&1; then
  # One submission at a time across the fleet: an atomic mkdir on the shared filesystem, taken whether or not the queue guards are
  # configured, because the budget check below depends on it. The holder refreshes the lock while it works (a cold budget read can
  # take minutes); a lock nobody has refreshed for FLEET_LOCK_STALE_S seconds (180) belongs to a dead holder and is taken over.
  STALE="${FLEET_LOCK_STALE_S:-180}"; GOT=0
  for _ in $(seq 1 "${FLEET_LOCK_WAIT:-45}"); do
    if mkdir "$LOCK" 2>/dev/null; then GOT=1; break; fi
    AGE=$(( $(date +%s) - $(stat -c %Y "$LOCK" 2>/dev/null || date +%s) ))
    if [ "$AGE" -gt "$STALE" ]; then rmdir "$LOCK" 2>/dev/null || true; continue; fi
    python3 -c "import time; time.sleep(1)"
  done
  [ $GOT = 1 ] || { echo "refused: another submission holds the fleet lock ($LOCK) for more than ${FLEET_LOCK_WAIT:-45} seconds; nothing was submitted; try again in a minute" >&2; exit 4; }
  BEAT=$(( STALE / 3 )); [ "$BEAT" -ge 1 ] || BEAT=1
  ( while kill -0 $$ 2>/dev/null; do touch "$LOCK" 2>/dev/null || true; sleep "$BEAT"; done ) >/dev/null 2>&1 &
  HB=$!
  trap 'kill $HB 2>/dev/null || true; rmdir "$LOCK" 2>/dev/null || true' EXIT
  LOCKED=1
  # jobs that were queued earlier go first: release what fits before this submission is looked at
  FLEET_LOCK_HELD=1 bash "$HERE/fleet_dispatch.sh" --site "$SITE" --quiet || true
fi
# The budget check runs inside the lock, after every earlier submission has been made and reserved, so two submissions can never both
# spend the same headroom. Its committed hours include the reservations of accepted jobs that the queue or accounting does not show yet.
if [ $OVERCAP = 0 ]; then FLEET_LOCK_HELD=$LOCKED FLEET_RESERVATIONS_FILE="$RFILE" python3 "$HERE/budget.py" --site "$SITE" --lane "$LANE" --est-hours "$EST" >/dev/null || { echo "refused by the budget check (worst case ${EST} GPU-h for lane $LANE); see slurm/budget.py --site $SITE, or --over-cap-ok"; exit 3; }; fi
echo "worst-case charge for this submission: ${EST} GPU-h (lane $LANE)" >&2
if [ $LOCKED = 1 ] && [ -n "${MAX_NODES_IN_FLIGHT:-}${NO_DUPLICATE_NAMES:-}${MAX_SUBMITS_PER_10MIN:-}" ]; then
  QSNAP=$(SLURM_BITSTR_LEN=0 squeue -A "$ACCOUNT" -h -t all -o '%D|%t|%E|%r|%V|%F|%j|%i' 2>/dev/null) || { echo "refused: the queue could not be read (squeue failed), so the guards cannot run; nothing was submitted; try again in a minute" >&2; exit 4; }
  qsnap() { printf '%s\n' "$QSNAP"; }
  if [ -n "${NO_DUPLICATE_NAMES:-}" ] && [ $ALLOW_SAME = 0 ]; then
    DUP=$(qsnap | awk -F'|' -v n="$NAME" '$7==n && $2 ~ /^(PD|R|CF|CG|S|RQ|RH|RF|RS|SI|SO|ST)$/ {c++} END{print c+0}')
    if [ "$DUP" -gt 0 ]; then echo "refused by the duplicate guard: a job named $NAME is already pending or running on the account (see: TZ=UTC squeue -A $ACCOUNT -n $NAME). If it is held, it is waiting for a node or for review: do not resubmit it. If a second copy is really intended, for example the next segment of the same run, add --allow-same-name." >&2; exit 5; fi
  fi
  if [ -n "${MAX_SUBMITS_PER_10MIN:-}" ]; then
    RECENT=$(qsnap | python3 -c "import sys,datetime as d
now=d.datetime.now(); ids=set()
for l in sys.stdin:
    f=l.rstrip().split('|')
    if len(f)>=7:
        try:
            if (now-d.datetime.fromisoformat(f[4])).total_seconds()<600: ids.add(f[5])
        except ValueError: pass
print(len(ids))")
    if [ "$RECENT" -ge "$MAX_SUBMITS_PER_10MIN" ]; then echo "refused by the rate guard: $RECENT submissions from the account in the last ten minutes (limit $MAX_SUBMITS_PER_10MIN). If a loop is submitting, stop it; if it is only a busy moment, wait a few minutes." >&2; exit 6; fi
  fi
  if [ -n "${MAX_NODES_IN_FLIGHT:-}" ] && { [ $CPU_ONLY = 0 ] || [ $FORCE_NODE = 1 ]; }; then
    CONC=1; if [ -n "$ARRAY" ]; then CONC="$THROTTLE"; fi
    WANT=$((NODES*CONC))
    if [ "$WANT" -gt "$MAX_NODES_IN_FLIGHT" ]; then echo "refused: this submission would use $WANT node(s) at once (nodes x array throttle); the limit is $MAX_NODES_IN_FLIGHT, held or chained or not" >&2; exit 4; fi
    if [ $HOLD = 0 ] && [ -z "$AFTER$AFTERANY" ]; then
      # nodes in flight (slurm/inflight.py, shared with the dispatcher): the pending part of an array counts the elements its throttle
      # lets run at once; an accepted job the queue does not show yet counts from its reservation
      INFLIGHT=$(qsnap | python3 "$HERE/inflight.py" --fields D,t,E,r,V,F,j,i --reservations "$RFILE") || { echo "refused: the nodes in flight could not be computed; nothing was submitted" >&2; exit 4; }
      if [ -s "$QFILE" ]; then WAITING=$(qsnap | awk -F'|' -v qf="$QFILE" 'BEGIN{while((getline l < qf)>0){split(l,a,"|"); q[a[2]]=1}} $2=="PD" && $4 ~ /JobHeldUser/ && ($6 in q) && !($6 in seen) {seen[$6]=1; c++} END{print c+0}'); fi
      if [ "$WAITING" -gt 0 ] || [ $((INFLIGHT+WANT)) -gt "$MAX_NODES_IN_FLIGHT" ]; then
        # the fleet is full, or others are already waiting: accept the job, hold it, and let the dispatcher start it in turn
        GUARD_QUEUED=1; ARGS+=(--hold)
      fi
    fi
  fi
fi
# one --dependency option (sbatch keeps only the last one if it is given twice); a chain that can never start is cancelled, not left pending
DEPS=""; [ -n "$AFTER" ] && DEPS="afterok:$AFTER"; [ -n "$AFTERANY" ] && DEPS="${DEPS:+$DEPS,}afterany:$AFTERANY"
if [ -n "$DEPS" ]; then ARGS+=(--dependency="$DEPS"); fi
if [ -n "$AFTER" ]; then ARGS+=(--kill-on-invalid-dep=yes); fi
echo "sbatch ${ARGS[*]} slurm/node.sbatch" >&2
JOBID=$(sbatch --parsable "${ARGS[@]}" "$JDIR/slurm/node.sbatch")
echo "$JOBID"
# reserve the job's worst case, still inside the lock, until the queue or accounting shows the job (budget.py drops it then)
# the reservation also carries the nodes this job uses at once, whether it was submitted held or chained (it then cannot start by
# itself), its task count, and an empty field for when the queue last showed it (the budget check fills it in)
HELDNOW=0; { [ $HOLD = 1 ] || [ $GUARD_QUEUED = 1 ] || [ -n "$DEPS" ]; } && HELDNOW=1
if [ "$EST" != 0 ]; then echo "$(date -u +%FT%TZ)|${JOBID%%;*}|$LANE|$EST|$TIME|$NAME|$WANT|$HELDNOW|$NTASKS|" >> "$RFILE"; fi
if [ $GUARD_QUEUED = 1 ]; then
  echo "$(date -u +%FT%TZ)|${JOBID%%;*}|$NAME|$WANT" >> "$QFILE"
  echo "QUEUED BY THE FLEET GUARD: job ${JOBID%%;*} is accepted and held, position $((WAITING+1)) in the fleet queue. It is released automatically, first in first out, when a node frees ($MAX_NODES_IN_FLIGHT at once). Do NOT resubmit it. Queue: $QFILE; the principal can also start it by hand with: scontrol release ${JOBID%%;*}" >&2
fi
