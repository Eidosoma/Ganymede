#!/bin/bash
# fleet_status.sh [--site NAME] [--hours 24]: one screen for the principal and the agents: budget per lane, the queue, the last
# day's finished jobs from accounting, and receipts (all_ok) from JOBS.log.
set -euo pipefail; HERE="$(cd "$(dirname "$0")" && pwd)"; JDIR="$(cd "$HERE/.." && pwd)"; SITE=local; HOURS=24
while [ $# -gt 0 ]; do case "$1" in --site) SITE="$2"; shift 2;; --hours) HOURS="$2"; shift 2;; *) shift;; esac; done
echo "# fleet status $(date -u +%FT%TZ) site=$SITE host=$(hostname)"
echo; echo "## budget (GPU-hours: used since BUDGET_SINCE, committed by the queue, cap, headroom; jobs running/pending)"; python3 "$HERE/budget.py" --site "$SITE"
echo; echo "## queue"; squeue -u "$USER" --Format="JobID:9,Name:30,Partition:14,StateCompact:4,TimeUsed:11,TimeLimit:11,NumCPUs:6,tres-per-node:14,Reason:24"
echo; echo "## waiting for a node (accepted and held by the wrapper, released first in first out; time UTC | job | name | nodes at once)"; if [ -s "$JDIR/receipts/FLEET_QUEUE.log" ]; then cat "$JDIR/receipts/FLEET_QUEUE.log"; else echo "(nobody)"; fi
SINCE=$(date -u -d "-${HOURS} hours" +%FT%T 2>/dev/null || date -u +%F)
echo; echo "## finished in the last ${HOURS} h (accounting)"; sacct -X -n -P -u "$USER" --starttime "$SINCE" --format=JobID,JobName,Partition,State,ElapsedRaw,AllocTRES | awk -F'|' '$4!="RUNNING" && $4!="PENDING" {gsub(/billing=[0-9]+,/,"",$6); printf "%8s %-28s %-12s %-10s %6ss %s\n",$1,$2,$3,$4,$5,$6}'
echo; echo "## receipts (JOBS.log, last ${HOURS} h)"; if [ -f "$JDIR/receipts/JOBS.log" ]; then awk -v since="$SINCE" '$1 >= since' "$JDIR/receipts/JOBS.log" | grep -v ' START ' || echo "(none)"; else echo "(no JOBS.log yet)"; fi
echo; echo "## summary"; if [ -f "$JDIR/receipts/JOBS.log" ]; then awk -v since="$SINCE" '$1 >= since && $2!="START" {n[$2]++} END {for (k in n) printf "%s=%d ", k, n[k]; print ""}' "$JDIR/receipts/JOBS.log"; fi
