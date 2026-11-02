#!/bin/bash
# One packed node: up to GPUS_PER_NODE single-GPU tasks plus CPU sidecars from one spec. Submit through submit.sh, which sets
# account, partition, gres, cpus, time and the USR1 signal from the site file; SPEC and RECEIPTS arrive through --export.
#SBATCH --output=logs/%x-%j.out
#SBATCH --error=logs/%x-%j.err
set -euo pipefail
cd "${FLEET_DIR:?}"
eval "${ENV_ACTIVATE:-:}"
mkdir -p logs receipts
JOBLOG="${RECEIPTS:-receipts}/JOBS.log"
if [ -n "${SLURM_ARRAY_TASK_ID:-}" ]; then SPEC="${SPEC_DIR:?}/node_${SLURM_ARRAY_TASK_ID}.json"; fi
RDIR="${RECEIPTS:-receipts}/${SLURM_JOB_NAME}-${SLURM_JOB_ID}${SLURM_ARRAY_TASK_ID:+-a$SLURM_ARRAY_TASK_ID}"
echo "node=$(hostname) spec=$SPEC receipts=$RDIR gpus=${SLURM_JOB_GPUS:-none} cpus=${SLURM_CPUS_ON_NODE:-?} cpu_only=${CPU_ONLY:-0} start=$(date -u +%FT%TZ)"
[ -n "${FORCE_NODE_REASON:-}" ] && echo "whole node held for CPU work: $FORCE_NODE_REASON"
echo "$(date -u +%FT%TZ) START job=$SLURM_JOB_ID name=$SLURM_JOB_NAME${SLURM_ARRAY_TASK_ID:+[$SLURM_ARRAY_TASK_ID]} node=$(hostname) receipt=$RDIR${FLEET_SBATCH_COMMENT:+ comment=$FLEET_SBATCH_COMMENT}" >> "$JOBLOG"
set +e
# Slurm's timed warning (--signal=B:USR1@N, set by submit.sh) reaches THIS shell only. Without a trap bash dies of it, the batch step
# ends and Slurm tears the job down before any task can checkpoint (it shows as ExitCode 0:10). So the
# launcher runs in the background, USR1 and TERM are forwarded to it (in multi-node mode to srun, which forwards them to every
# node's launcher; `exec` makes the launcher the srun task itself), and the shell keeps waiting until the launcher has really exited.
if [ "${MULTI_NODES:-1}" -gt 1 ]; then
  # one launcher per node, same spec everywhere; tasks see SLURM_NNODES / SLURM_NODEID / SLURM_JOB_NODELIST for rendezvous
  srun --nodes="$MULTI_NODES" --ntasks="$MULTI_NODES" --ntasks-per-node=1 --cpus-per-task="${SLURM_CPUS_PER_TASK:-1}" \
       bash -c 'exec python3 pack_node.py --spec "$SPEC" --receipt-dir "'"$RDIR"'/node-$SLURM_NODEID" '"${PACK_EXTRA:-} ${PACK_ARGS:-}"'' &
else
  python3 pack_node.py --spec "$SPEC" --receipt-dir "$RDIR" ${PACK_EXTRA:-} ${PACK_ARGS:-} &
fi
LPID=$!
trap 'echo "$(date -u +%FT%TZ) batch shell: USR1 from Slurm, forwarded to the launcher (pid $LPID)"; kill -USR1 "$LPID" 2>/dev/null' USR1
trap 'echo "$(date -u +%FT%TZ) batch shell: TERM, forwarded to the launcher (pid $LPID)"; kill -TERM "$LPID" 2>/dev/null' TERM
RC=255
while :; do wait "$LPID"; RC=$?; kill -0 "$LPID" 2>/dev/null || break; done   # a trapped signal interrupts wait; wait again until the launcher is gone
trap - USR1 TERM
set -e
STATUS=DONE; [ $RC -eq 0 ] || STATUS=FAILED; [ $RC -eq 2 ] && STATUS=REFUSED
echo "$(date -u +%FT%TZ) $STATUS job=$SLURM_JOB_ID name=$SLURM_JOB_NAME${SLURM_ARRAY_TASK_ID:+[$SLURM_ARRAY_TASK_ID]} rc=$RC receipt=$RDIR/receipt.json" >> "$JOBLOG"
# This node is about to free: start the next job the wrapper queued, first in first out. Best effort: it never fails or delays
# this job, and the same pass also runs at every submission, so a job that dies without reaching this line only delays the queue.
( timeout 90 bash "$FLEET_DIR/slurm/fleet_dispatch.sh" --site "${FLEET_SITE:-local}" --departing "$SLURM_JOB_ID" >> logs/fleet_dispatch.out 2>&1 ) || true
exit $RC
