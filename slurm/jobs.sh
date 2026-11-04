#!/bin/bash
# jobs.sh <lane-tag|all> [--since YYYY-MM-DD]: the lane's jobs in the queue and in accounting, with receipt status. A lane's jobs are
# the ones whose names begin with its tag and a hyphen, so alpha is not alpha2. A source that cannot be read is named in its section
# and the rest is still shown, with exit status 1.
set -uo pipefail; HERE="$(cd "$(dirname "$0")" && pwd)"; JDIR="$(cd "$HERE/.." && pwd)"; TAG=''; SINCE=''; RC=0
usage(){ echo "usage: jobs.sh <lane-tag|all> [--since YYYY-MM-DD]" >&2; exit 2; }
while [ $# -gt 0 ]; do case "$1" in --since) [ $# -ge 2 ] || usage; SINCE="$2"; shift 2;; -*) echo "jobs.sh: unknown option: $1" >&2; usage;; *) [ -z "$TAG" ] || usage; TAG="$1"; shift;; esac; done
TAG="${TAG:-all}"; SINCE="${SINCE:-$(date -u -d '-2 days' +%F 2>/dev/null || date -u +%F)}"
[[ "$SINCE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || { echo "jobs.sh: --since takes a date, YYYY-MM-DD, not '$SINCE'" >&2; exit 2; }
ME="${USER:-$(id -un)}"   # a cron job or a service may run without USER
miss(){ echo "($1 failed, exit $2: not shown)"; RC=1; }
echo "== queue (pending/running)"
if Q=$(squeue -u "$ME" --Format="JobID:9,Name:30,Partition:14,StateCompact:4,TimeUsed:11,NumCPUs:6,Reason:24"); then
  printf '%s\n' "$Q" | awk -v tag="$TAG" 'NR==1 || tag=="all" || index(substr($0,10,30), tag "-")==1'   # the name: the 30 columns after the id's 9
else miss squeue $?; fi
echo "== accounting since $SINCE"
if A=$(sacct -X -n -P -u "$ME" --starttime "$SINCE" --format=JobID,JobName,Partition,State,ElapsedRaw,AllocTRES); then
  printf '%s\n' "$A" | awk -F'|' -v tag="$TAG" 'NF>=6 && (tag=="all" || index($2, tag "-")==1) {gsub(/billing=[0-9]+,/,"",$6); printf "%8s %-28s %-12s %-10s %6ss %s\n",$1,$2,$3,$4,$5,$6}'
else miss sacct $?; fi
echo "== receipts (JOBS.log)"
if [ -f "$JDIR/receipts/JOBS.log" ]; then
  awk -v tag="$TAG" '{n=""; for (i=3; i<=NF; i++) if (substr($i,1,5)=="name=") {n=substr($i,6); break}} tag=="all" || index(n, tag "-")==1' "$JDIR/receipts/JOBS.log" | tail -20 | grep . || echo "(none)"
else echo "(none)"; fi
exit $RC
