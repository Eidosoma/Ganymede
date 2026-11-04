#!/bin/bash
# fleet_status.sh [--site NAME] [--hours 24]: one screen for the principal and the agents: budget per lane, the queue, the jobs waiting
# for a node, the last day's finished jobs from accounting, and receipts (all_ok) from JOBS.log. A source that cannot be read (the
# queue, accounting) is named in its section and the rest of the screen is still shown, with exit status 1. The jobs waiting for a node
# are those of the fleet queue the site file names, the one submit.sh and the dispatcher use, so every copy of the tooling shows the
# same list.
set -uo pipefail; HERE="$(cd "$(dirname "$0")" && pwd)"; JDIR="$(cd "$HERE/.." && pwd)"; SITE=local; HOURS=24; RC=0
usage(){ echo "usage: fleet_status.sh [--site NAME] [--hours N]" >&2; exit 2; }
while [ $# -gt 0 ]; do case "$1" in --site) [ $# -ge 2 ] || usage; SITE="$2"; shift 2;; --hours) [ $# -ge 2 ] || usage; HOURS="$2"; shift 2;; *) echo "fleet_status.sh: unknown argument: $1" >&2; usage;; esac; done
[[ "$HOURS" =~ ^[0-9]+$ ]] || { echo "fleet_status.sh: --hours takes a whole number of hours, not '$HOURS'" >&2; exit 2; }
[ -f "$HERE/site.$SITE.env" ] || { echo "fleet_status.sh: no site file slurm/site.$SITE.env" >&2; exit 2; }
QFILE=$(set +u; . "$HERE/site.$SITE.env" >/dev/null 2>&1; printf '%s' "${FLEET_QUEUE_FILE:-}"); QFILE="${QFILE:-$JDIR/receipts/FLEET_QUEUE.log}"
ME="${USER:-$(id -un)}"   # a cron job or a service may run without USER
miss(){ echo "($1 failed, exit $2: not shown)"; RC=1; }
echo "# fleet status $(date -u +%FT%TZ) site=$SITE host=$(hostname)"
echo; echo "## budget (GPU-hours: used since BUDGET_SINCE, committed by the queue, cap, headroom; jobs running/pending)"; python3 "$HERE/budget.py" --site "$SITE" || miss budget.py $?
echo; echo "## queue"; squeue -u "$ME" --Format="JobID:9,Name:30,Partition:14,StateCompact:4,TimeUsed:11,TimeLimit:11,NumCPUs:6,tres-per-node:14,Reason:24" || miss squeue $?
echo; echo "## waiting for a node (accepted and held by the wrapper, released first in first out; time UTC | job | name | nodes at once | for a chained job, what it waits for)"; if [ -s "$QFILE" ]; then cat "$QFILE"; else echo "(nobody)"; fi
SINCE=$(date -u -d "-${HOURS} hours" +%FT%T 2>/dev/null || date -u +%F)
echo; echo "## finished in the last ${HOURS} h (accounting)"
if A=$(sacct -X -n -P -u "$ME" --starttime "$SINCE" --format=JobID,JobName,Partition,State,ElapsedRaw,AllocTRES); then
  printf '%s\n' "$A" | awk -F'|' 'NF>=6 && $4!="RUNNING" && $4!="PENDING" {gsub(/billing=[0-9]+,/,"",$6); printf "%8s %-28s %-12s %-10s %6ss %s\n",$1,$2,$3,$4,$5,$6}'
else miss sacct $?; fi
echo; echo "## receipts (JOBS.log, last ${HOURS} h)"; if [ -f "$JDIR/receipts/JOBS.log" ]; then awk -v since="$SINCE" '$1 >= since' "$JDIR/receipts/JOBS.log" | grep -v ' START ' || echo "(none)"; else echo "(no JOBS.log yet)"; fi
echo; echo "## summary"; if [ -f "$JDIR/receipts/JOBS.log" ]; then awk -v since="$SINCE" '$1 >= since && $2!="START" {n[$2]++} END {for (k in n) printf "%s=%d ", k, n[k]; print ""}' "$JDIR/receipts/JOBS.log"; fi
exit $RC
