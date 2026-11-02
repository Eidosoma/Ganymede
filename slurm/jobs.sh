#!/bin/bash
# jobs.sh <lane-tag|all> [--since YYYY-MM-DD]: the lane's jobs in the queue and in accounting, with receipt status.
set -euo pipefail; HERE="$(cd "$(dirname "$0")" && pwd)"; JDIR="$(cd "$HERE/.." && pwd)"
TAG="${1:-all}"; SINCE="${3:-$(date -u -d '-2 days' +%F 2>/dev/null || date -u +%F)}"
echo "== queue (pending/running)"; squeue -u "$USER" --Format="JobID:9,Name:30,Partition:14,StateCompact:4,TimeUsed:11,NumCPUs:6,Reason:24" | { read h; echo "$h"; grep -E "^ *[0-9_]+ +${TAG/all/.*}" || true; }
echo "== accounting since $SINCE"; sacct -X -n -P -u "$USER" --starttime "$SINCE" --format=JobID,JobName,Partition,State,ElapsedRaw,AllocTRES | awk -F'|' -v tag="$TAG" '$2 ~ "^"tag || tag=="all" {gsub(/billing=[0-9]+,/,"",$6); printf "%8s %-28s %-12s %-10s %6ss %s\n",$1,$2,$3,$4,$5,$6}'
echo "== receipts (JOBS.log)"; [ -f "$JDIR/receipts/JOBS.log" ] && grep -E "name=${TAG/all/.*}" "$JDIR/receipts/JOBS.log" | tail -20 || echo "(none)"
