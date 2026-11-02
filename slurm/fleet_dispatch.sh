#!/bin/bash
# fleet_dispatch.sh [--site NAME] [--departing JOBID] [--quiet]: release jobs that the submit wrapper queued, first in first out,
# as far as they fit under MAX_NODES_IN_FLIGHT.
#
# Why: Slurm may enforce no node limit on a project account, so a job that enters the queue unheld starts at once, beyond the
# fleet's ceiling. Refusing it would make the agent come back and try again, and agents that retry are how runaways start. So the
# wrapper submits it HELD,
# writes it to receipts/FLEET_QUEUE.log, and this script releases it when a node frees. It runs (a) at the end of every fleet job,
# from slurm/node.sbatch, with --departing so the leaving job is not counted; (b) at the start of every submission; (c) by hand or
# from an operator's session at any time. It is idempotent and safe to run as often as wanted.
#
# What it can and cannot do: it only ever RELEASES jobs that the wrapper itself queued (listed in FLEET_QUEUE.log and still pending
# with a user hold). It never submits, never cancels, never touches a job held for review or held by a lane (those are not in
# the list). It fails closed: if the queue cannot be read, or the lock cannot be taken, it does nothing. Strict first in first out:
# if the job at the head does not fit, nothing behind it is released.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; JDIR="$(cd "$HERE/.." && pwd)"
SITE=local; DEPARTING=""; QUIET=0
while [ $# -gt 0 ]; do case "$1" in --site) SITE="$2"; shift 2;; --departing) DEPARTING="$2"; shift 2;; --quiet) QUIET=1; shift;; *) shift;; esac; done
. "$HERE/site.$SITE.env"
QFILE="${FLEET_QUEUE_FILE:-$JDIR/receipts/FLEET_QUEUE.log}"; LOCK="${FLEET_LOCK_DIR:-$JDIR/receipts/.fleet.lock}"
say() { [ $QUIET = 1 ] || echo "$@"; }
[ -n "${MAX_NODES_IN_FLIGHT:-}" ] || { say "no MAX_NODES_IN_FLIGHT for site $SITE: nothing to do"; exit 0; }
[ -s "$QFILE" ] || { say "fleet queue empty"; exit 0; }
command -v squeue >/dev/null 2>&1 && command -v scontrol >/dev/null 2>&1 || { say "no Slurm commands here: nothing done"; exit 0; }
# lock: an atomic mkdir on the shared filesystem; a lock older than three minutes is stale (its holder died) and is taken over
unlock() { rmdir "$LOCK" 2>/dev/null || true; }
if [ "${FLEET_LOCK_HELD:-0}" != 1 ]; then
  GOT=0
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    if mkdir "$LOCK" 2>/dev/null; then GOT=1; break; fi
    AGE=$(( $(date +%s) - $(stat -c %Y "$LOCK" 2>/dev/null || date +%s) ))
    if [ "$AGE" -gt 180 ]; then rmdir "$LOCK" 2>/dev/null || true; continue; fi
    python3 -c "import time; time.sleep(1)"
  done
  [ $GOT = 1 ] || { say "fleet lock busy: nothing done (the next pass will do it)"; exit 0; }
  trap unlock EXIT
fi
SNAP=$(squeue -A "$ACCOUNT" -h -t PD,R,CF,CG -o '%A|%D|%t|%E|%r|%F|%j' 2>/dev/null) || { say "the queue could not be read: nothing done"; exit 0; }
# nodes in flight: running and configuring jobs, pending jobs that are free to start, and, when a job is leaving, its chained
# successors (they start the moment it ends); the leaving job itself is not counted
INFLIGHT=$(printf '%s\n' "$SNAP" | awk -F'|' -v dep="$DEPARTING" '
  $1==dep {next}
  $3=="R"||$3=="CF" {n+=$2; next}
  $3=="PD" && ($4==""||$4=="(null)") && $5!~/Held/ {n+=$2; next}
  $3=="PD" && dep!="" && $5!~/Held/ && $4 ~ ("(^|[:,?])" dep "([(:,?_]|$)") {n+=$2}
  END {print n+0}')
RELEASED=0; RELIDS=""
while IFS='|' read -r TS JID NAME NODES _; do
  [ -n "${JID:-}" ] || continue
  HELDNOW=$(printf '%s\n' "$SNAP" | awk -F'|' -v j="$JID" '$6==j && $3=="PD" && $5 ~ /JobHeldUser/ {c++} END{print c+0}')
  [ "$HELDNOW" -gt 0 ] || continue                             # not held any more: released earlier, running, cancelled or finished
  N="${NODES:-1}"; case "$N" in ''|*[!0-9]*) N=1;; esac          # nodes this job uses at once (nodes x array throttle), as the wrapper recorded it
  if [ $((INFLIGHT+N)) -le "$MAX_NODES_IN_FLIGHT" ]; then
    if scontrol release "$JID" 2>/dev/null; then
      INFLIGHT=$((INFLIGHT+N)); RELEASED=$((RELEASED+1)); RELIDS="$RELIDS $JID"
      echo "$(date -u +%FT%TZ) RELEASED job=$JID name=$NAME nodes=$N in_flight_after=$INFLIGHT${DEPARTING:+ departing=$DEPARTING}" | tee -a "${FLEET_DISPATCH_LOG:-$JDIR/receipts/FLEET_DISPATCH.log}" >/dev/null
      say "released $JID ($NAME)"
    else say "release of $JID failed; left held"; break; fi
  else break; fi                                               # strict first in first out
done < "$QFILE"
# keep the list short: drop entries whose job is no longer pending with a hold (it was released, cancelled, or has run). Safe under
# the lock, and only after a successful reading of the queue: a held job never leaves the queue by itself.
TMPQ="$QFILE.tmp.$$"
awk -F'|' -v snap="$SNAP" -v rel="$RELIDS" 'BEGIN{n=split(snap,L,"\n"); for(i=1;i<=n;i++){split(L[i],f,"|"); if(f[3]=="PD" && f[5] ~ /JobHeldUser/) held[f[6]]=1}; m=split(rel,R," "); for(i=1;i<=m;i++) delete held[R[i]]} ($2 in held)' "$QFILE" > "$TMPQ" 2>/dev/null && mv "$TMPQ" "$QFILE" || rm -f "$TMPQ"
say "dispatch pass done: $RELEASED released, $INFLIGHT node(s) in flight, limit $MAX_NODES_IN_FLIGHT, $(wc -l < "$QFILE" 2>/dev/null || echo 0) still waiting"
exit 0
