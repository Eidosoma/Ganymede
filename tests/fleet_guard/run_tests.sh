#!/bin/bash
# tests/fleet_guard/run_tests.sh [TOOLING_DIR]: the submit wrapper's guards, the fleet queue and its dispatcher, the budget check and
# wait_jobs.py, run against a FAKE Slurm (bin/: squeue, sbatch, scontrol, sacct over a state file), then the launcher's GPU-idle
# watchdog (test_watchdog.sh) and the scoped fast-forward (test_ffscoped.sh). Nothing here can reach a real scheduler: PATH puts the
# fakes first, and the tooling under test is a temporary copy with the test site file (site.test.env: four nodes, review threshold 50)
# and the test caps (caps.test.json), so no live receipt, log, queue or caps file is touched.
# Run it before every release: bash tests/fleet_guard/run_tests.sh   (last line: RESULT: N passed, 0 failed)
HERE="$(cd "$(dirname "$0")" && pwd)"; TOOLING="${1:-$(cd "$HERE/../.." && pwd)}"
T=$(mktemp -d "${TMPDIR:-/tmp}/fleet_guard_test.XXXXXX"); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/stage"; cp -r "$TOOLING/slurm" "$T/stage/"; cp "$TOOLING/VERSION" "$T/stage/"; cp -r "$HERE/bin" "$T/bin"; cp "$HERE/spec.json" "$T/spec.json"
STAGE="$T/stage"; export FAKE="$T/state"; export PATH="$T/bin:$PATH"
cp "$HERE/site.test.env" "$STAGE/slurm/site.test.env" && cp "$HERE/caps.test.json" "$STAGE/caps.json" || { echo "could not stage the test site file and caps"; exit 2; }
export FLEET_QUEUE_FILE="$T/state/FLEET_QUEUE.log" FLEET_LOCK_DIR="$T/state/.fleet.lock" FLEET_DISPATCH_LOG="$T/dispatch.log" FLEET_LOCK_WAIT=3
PASS=0; FAILS=0
ok()   { PASS=$((PASS+1)); echo "  pass: $1"; }
bad()  { FAILS=$((FAILS+1)); echo "  FAIL: $1"; }
check(){ if eval "$2"; then ok "$1"; else bad "$1  [$2]"; fi; }
reset(){ rm -rf "$FAKE"; mkdir -p "$FAKE"; rm -rf "$STAGE/receipts" "$STAGE/logs"; }
job()  { echo "$1|$1|${2:-1}|${3:-R}|${4:-(null)}|${5:-None}|${6:-2026-09-20T10:00:00}|${7:-other-job-$1}|1:00:00|0:10" >> "$FAKE/jobs.txt"; }
sub()  { OUT=$(bash "$STAGE/slurm/submit.sh" --site test --time 01:00:00 "$@" "$T/spec.json" 2>"$T/state/err.txt"); RC=$?; ERR=$(cat "$T/state/err.txt"); }
calls(){ cat "$FAKE/sbatch_calls.txt" 2>/dev/null; }

echo "1. room in the fleet: a plain submission goes straight in"
reset; job 101; job 102; sub
check "exit 0 and a bare job id on stdout" '[ $RC = 0 ] && [[ "$OUT" =~ ^[0-9]+$ ]]'
check "not held, not queued" '! calls | grep -q -- "--hold" && [ ! -s "$FLEET_QUEUE_FILE" ]'
check "submitted --no-requeue" 'calls | grep -q -- "--no-requeue"'

echo "2. fleet full: the job is accepted and held, not refused"
reset; job 101; job 102; job 103; job 104; sub
check "exit 0 and a bare job id" '[ $RC = 0 ] && [[ "$OUT" =~ ^[0-9]+$ ]]'
check "submitted with --hold" 'calls | grep -q -- "--hold"'
check "written to the fleet queue with its node count" 'grep -q "|$OUT|alpha-test-v1|1$" "$FLEET_QUEUE_FILE"'
check "the message says do not resubmit" 'echo "$ERR" | grep -q "Do NOT resubmit"'
Q1=$OUT

echo "3. the same job again is refused by the duplicate guard"
sub
check "exit 5" '[ $RC = 5 ]'
check "only one sbatch call so far" '[ $(calls | wc -l) = 1 ]'
sub --allow-same-name --name alpha-test-v1
check "--allow-same-name lets a deliberate second copy in (queued behind the first)" '[ $RC = 0 ] && [ $(wc -l < "$FLEET_QUEUE_FILE") = 2 ]'
Q2=$OUT

echo "4. a node frees: the dispatcher releases the head of the queue only"
grep -v "^101|" "$FAKE/jobs.txt" > "$FAKE/j" && mv "$FAKE/j" "$FAKE/jobs.txt"
bash "$STAGE/slurm/fleet_dispatch.sh" --site test --quiet
check "first queued job released" 'grep -q "^release $Q1$" "$FAKE/scontrol_calls.txt"'
check "second queued job still held" '! grep -q "^release $Q2$" "$FAKE/scontrol_calls.txt"'
bash "$STAGE/slurm/fleet_dispatch.sh" --site test --quiet
check "a second pass changes nothing (idempotent)" '[ $(wc -l < "$FAKE/scontrol_calls.txt") = 1 ]'

echo "5. a newcomer does not jump the queue when a node frees"
grep -v "^102|" "$FAKE/jobs.txt" > "$FAKE/j" && mv "$FAKE/j" "$FAKE/jobs.txt"
sub --name alpha-newcomer-v1
check "the waiting job was released first (by the pass at the start of the submission)" 'grep -q "^release $Q2$" "$FAKE/scontrol_calls.txt"'
check "the newcomer is held behind it" 'calls | tail -1 | grep -q -- "--hold" && grep -q "|alpha-newcomer-v1|" "$FLEET_QUEUE_FILE"'

echo "6. a leaving job: itself not counted, its chained successor counted"
reset; job 101; job 102; job 103; job 104; job 105 1 PD "afterany:104(unfulfilled)" Dependency; sub; Q=$OUT
bash "$STAGE/slurm/fleet_dispatch.sh" --site test --departing 104 --quiet
check "104 leaves but its successor 105 takes the node: nothing released" '[ ! -s "$FAKE/scontrol_calls.txt" ]'
bash "$STAGE/slurm/fleet_dispatch.sh" --site test --departing 103 --quiet
check "103 leaves with no successor: the queued job is released" 'grep -q "^release $Q$" "$FAKE/scontrol_calls.txt"'

echo "7. strict first in first out: a two-node job at the head is not overtaken"
reset; job 101; job 102; job 103; job 104; sub --nodes 2 --name infra-two-v1; QA=$OUT; sub --name alpha-one-v1; QB=$OUT
grep -v "^101|" "$FAKE/jobs.txt" > "$FAKE/j" && mv "$FAKE/j" "$FAKE/jobs.txt"
bash "$STAGE/slurm/fleet_dispatch.sh" --site test --quiet
check "one node free, head needs two: nothing released" '[ ! -s "$FAKE/scontrol_calls.txt" ]'
grep -v "^102|" "$FAKE/jobs.txt" > "$FAKE/j" && mv "$FAKE/j" "$FAKE/jobs.txt"
bash "$STAGE/slurm/fleet_dispatch.sh" --site test --quiet
check "two nodes free: the two-node job goes, the one-node job waits" 'grep -q "^release $QA$" "$FAKE/scontrol_calls.txt" && ! grep -q "^release $QB$" "$FAKE/scontrol_calls.txt"'

echo "8. held for review is never auto-released"
reset; job 101; sub --time 12:00:00 --array 0-8%1 --spec-dir "$T/specdir" 2>/dev/null
mkdir -p "$T/specdir"; for i in 0 1 2 3 4 5 6 7 8; do cp "$T/spec.json" "$T/specdir/node_$i.json"; done
reset; job 101; OUT=$(bash "$STAGE/slurm/submit.sh" --site test --time 12:00:00 --array 0-8%1 --spec-dir "$T/specdir" 2>"$T/state/err.txt"); RC=$?; ERR=$(cat "$T/state/err.txt")
check "a 432 GPU-hour array is held for review" '[ $RC = 0 ] && echo "$ERR" | grep -q "HELD FOR REVIEW"'
check "and is not in the fleet queue" '[ ! -s "$FLEET_QUEUE_FILE" ]'
bash "$STAGE/slurm/fleet_dispatch.sh" --site test --quiet
check "the dispatcher leaves it held" '[ ! -s "$FAKE/scontrol_calls.txt" ]'

echo "9. arrays must carry a throttle that fits"
reset; OUT=$(bash "$STAGE/slurm/submit.sh" --site test --time 00:10:00 --array 0-8 --spec-dir "$T/specdir" 2>/dev/null); check "no throttle: exit 2" '[ $? = 2 ]'
OUT=$(bash "$STAGE/slurm/submit.sh" --site test --time 00:10:00 --hold --array 0-8%5 --spec-dir "$T/specdir" 2>/dev/null); check "throttle 5, even held: exit 4" '[ $? = 4 ]'
OUT=$(bash "$STAGE/slurm/submit.sh" --site test --time 00:10:00 --array 0-8%1 --spec-dir "$T/specdir" 2>/dev/null); check "throttle 1: accepted" '[ $? = 0 ]'
check "nothing was submitted by the two refusals" '[ $(calls | wc -l) = 1 ]'

echo "10. fail closed when the scheduler does not answer"
reset; job 101; touch "$FAKE/squeue_fail"; sub
check "submission refused (exit 3 or 4), nothing sent" '{ [ $RC = 4 ] || [ $RC = 3 ]; } && [ ! -s "$FAKE/sbatch_calls.txt" ]'
echo "2026-09-20T17:00:00Z|777|alpha-held-v1|1" > "$FLEET_QUEUE_FILE"; bash "$STAGE/slurm/fleet_dispatch.sh" --site test --quiet
check "the dispatcher releases nothing" '[ ! -s "$FAKE/scontrol_calls.txt" ]'
rm -f "$FAKE/squeue_fail"; touch "$FAKE/sacct_fail"; rm -f "$FLEET_QUEUE_FILE"; sub
check "accounting down: refused by the budget check (exit 3), nothing sent" '[ $RC = 3 ] && [ ! -s "$FAKE/sbatch_calls.txt" ]'

echo "11. a job name with an unknown lane tag has no cap and is refused"
reset; sub --name mystery-job-v1
check "exit 3, nothing sent" '[ $RC = 3 ] && [ ! -s "$FAKE/sbatch_calls.txt" ]'

echo "12. the rate guard"
reset; NOW=$(date +%FT%T); for i in $(seq 201 212); do job $i 1 CD "(null)" None "$NOW" "beta-burst-$i"; done; sub
check "12 submissions in the last ten minutes: exit 6, nothing sent" '[ $RC = 6 ] && [ ! -s "$FAKE/sbatch_calls.txt" ]'
reset; for i in $(seq 201 212); do job $i 1 CD "(null)" None "2026-09-20T09:00:00" "beta-old-$i"; done; sub
check "12 old submissions do not count" '[ $RC = 0 ]'

echo "13. dependencies"
reset; job 101; sub --after 101 --afterany 102
check "one --dependency with both kinds" 'calls | grep -q -- "--dependency=afterok:101,afterany:102" && [ $(calls | grep -o -- "--dependency" | wc -l) = 1 ]'
check "a chain that can never start is cancelled" 'calls | grep -q -- "--kill-on-invalid-dep=yes"'

echo "14. the fleet lock"
reset; mkdir -p "$FLEET_LOCK_DIR"; sub
check "a fresh lock held by someone else: refused after the wait (exit 4), nothing sent" '[ $RC = 4 ] && [ ! -s "$FAKE/sbatch_calls.txt" ]'
touch -d "10 minutes ago" "$FLEET_LOCK_DIR"; sub
check "a stale lock is taken over" '[ $RC = 0 ]'
check "the lock is gone after the submission" '[ ! -d "$FLEET_LOCK_DIR" ]'

echo "15. a failed sbatch leaves no trace in the queue file and frees the lock"
reset; job 101; job 102; job 103; job 104; touch "$FAKE/sbatch_fail"; sub
check "non-zero exit, no queue entry, no lock left" '[ $RC != 0 ] && [ ! -s "$FLEET_QUEUE_FILE" ] && [ ! -d "$FLEET_LOCK_DIR" ]'

echo "16. wait_jobs.py"
reset; job 301; touch "$FAKE/squeue_fail"
timeout 8 python3 "$STAGE/slurm/wait_jobs.py" 301 --poll 1 >/dev/null 2>&1; check "squeue failing: it keeps waiting (stopped by the test's timeout, code 124)" '[ $? = 124 ]'
rm -f "$FAKE/squeue_fail"; : > "$FAKE/jobs.txt"; echo "301|other-job-301|RUNNING|100" > "$FAKE/sacct.txt"
timeout 8 python3 "$STAGE/slurm/wait_jobs.py" 301 --poll 1 >/dev/null 2>&1; check "queue empty but accounting still says RUNNING: it keeps waiting" '[ $? = 124 ]'
echo "301|other-job-301|COMPLETED|100" > "$FAKE/sacct.txt"
timeout 40 python3 "$STAGE/slurm/wait_jobs.py" 301 --poll 1 >/dev/null 2>&1; check "queue empty and accounting terminal: it returns" '[ $? != 124 ]'
wj_consts(){ python3 - "$1" <<'PYEOF'
import importlib.util, sys
s = importlib.util.spec_from_file_location('w', sys.argv[1]); m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
print(m.DEFAULT_POLL, m.MIN_POLL)
PYEOF
}
check "it polls every ten minutes by default and never faster than once a minute" '[ "$(wj_consts "$STAGE/slurm/wait_jobs.py")" = "600.0 60.0" ]'

echo "17. the queue file is pruned and the dispatcher stays quiet when nobody waits"
reset; job 101; job 102; job 103; job 104; sub --name alpha-prune-a; QA=$OUT; sub --name alpha-prune-b; QB=$OUT
grep -v "^101|" "$FAKE/jobs.txt" > "$FAKE/j" && mv "$FAKE/j" "$FAKE/jobs.txt"
bash "$STAGE/slurm/fleet_dispatch.sh" --site test --quiet
check "after the head is released only the second job is left in the file" '[ $(wc -l < "$FLEET_QUEUE_FILE") = 1 ] && grep -q "|$QB|" "$FLEET_QUEUE_FILE"'
grep -v "^$QB|" "$FAKE/jobs.txt" > "$FAKE/j" && mv "$FAKE/j" "$FAKE/jobs.txt"   # the lane cancels its waiting job
bash "$STAGE/slurm/fleet_dispatch.sh" --site test --quiet
check "a cancelled waiting job is dropped from the file" '[ ! -s "$FLEET_QUEUE_FILE" ]'
touch "$FAKE/squeue_fail"; echo "2026-09-20T17:00:00Z|999|alpha-x|1" > "$FLEET_QUEUE_FILE"; bash "$STAGE/slurm/fleet_dispatch.sh" --site test --quiet
check "no pruning when the queue cannot be read" '[ $(wc -l < "$FLEET_QUEUE_FILE") = 1 ]'

echo "18. two copies of the tooling share one queue, one lock and one release log (a lane's pinned copy)"
reset; SH="$T/shared"; mkdir -p "$SH"; rm -rf "$T/copy2"; cp -r "$STAGE" "$T/copy2"
for c in "$STAGE" "$T/copy2"; do sed -i -E "s#^(FLEET_QUEUE_FILE=.*:-)[^}]*#\\1$SH/FLEET_QUEUE.log#; s#^(FLEET_LOCK_DIR=.*:-)[^}]*#\\1$SH/.fleet.lock#; s#^(FLEET_DISPATCH_LOG=.*:-)[^}]*#\\1$SH/FLEET_DISPATCH.log#" "$c/slurm/site.test.env"; done
job 101; job 102; job 103; job 104
OUT=$(env -u FLEET_QUEUE_FILE -u FLEET_LOCK_DIR -u FLEET_DISPATCH_LOG bash "$T/copy2/slurm/submit.sh" --site test --time 01:00:00 --name gamma-pinned-copy-v1 "$T/spec.json" 2>/dev/null); RC=$?
check "a job queued through the second copy lands in the shared list, not in the copy's own folder" '[ $RC = 0 ] && grep -q "|$OUT|gamma-pinned-copy-v1|" "$SH/FLEET_QUEUE.log" && [ ! -e "$T/copy2/receipts/FLEET_QUEUE.log" ]'
grep -v "^101|" "$FAKE/jobs.txt" > "$FAKE/j" && mv "$FAKE/j" "$FAKE/jobs.txt"
env -u FLEET_QUEUE_FILE -u FLEET_LOCK_DIR -u FLEET_DISPATCH_LOG bash "$STAGE/slurm/fleet_dispatch.sh" --site test --quiet
check "the first copy's dispatcher releases it and writes the shared release log" 'grep -q "^release $OUT$" "$FAKE/scontrol_calls.txt" && grep -q "job=$OUT " "$SH/FLEET_DISPATCH.log"'
export FLEET_QUEUE_FILE="$T/state/FLEET_QUEUE.log" FLEET_LOCK_DIR="$T/state/.fleet.lock"

echo "19. the launcher's GPU-idle watchdog: fake mode with PACK_FAKE_GPU_UTIL, tests/fleet_guard/test_watchdog.sh"
if bash "$HERE/test_watchdog.sh" "$TOOLING/pack_node.py" > "$T/wd.out" 2>&1; then ok "watchdog suite: $(tail -1 "$T/wd.out")"; else bad "watchdog suite: $(tail -1 "$T/wd.out")"; cat "$T/wd.out"; fi

echo "20. the scoped fast-forward of the shared checkout: throwaway repositories, tests/fleet_guard/test_ffscoped.sh"
if TMPDIR="$T" bash "$HERE/test_ffscoped.sh" "$TOOLING/git/ffscoped.py" > "$T/ff.out" 2>&1; then ok "ffscoped suite: $(tail -1 "$T/ff.out")"; else bad "ffscoped suite: $(tail -1 "$T/ff.out")"; cat "$T/ff.out"; fi

echo "21. the budget check: accounting for the account only, in UTC windows of at most seven days, each job once, finished windows from a cache"
reset; NAME=alpha-budget-v1; ACCT=$(. "$STAGE/slurm/site.test.env"; echo "$ACCOUNT")
printf '%s\n' "401|$NAME|1|billing=288,cpu=288,gres/gpu=4,node=1|3600|COMPLETED" > "$FAKE/sacct.txt"
used_of(){ python3 -c 'import json,sys; d=json.load(sys.stdin); print([r["used"] for r in d if r["lane"]==sys.argv[1]][0])' "$1"; }
win_ok(){ python3 - "$1" <<'PYEOF'
import re, sys, datetime as d
ok = True
for l in open(sys.argv[1]):
    m = re.search(r'--starttime (\S+) --endtime (\S+)', l)
    ok = ok and bool(m) and 0 < (d.datetime.fromisoformat(m.group(2)) - d.datetime.fromisoformat(m.group(1))).total_seconds() <= 7 * 86400
sys.exit(0 if ok else 1)
PYEOF
}
U=$(python3 "$STAGE/slurm/budget.py" --site test --json 2>/dev/null | used_of "${NAME%%-*}"); N1=$(wc -l < "$FAKE/sacct_calls.txt")
check "a job that every window returns is counted once (4 GPU-hours)" '[ "$U" = 4.0 ]'
check "every accounting read names the account" '[ "$N1" -ge 2 ] && ! grep -v -- "-A $ACCT " "$FAKE/sacct_calls.txt" | grep -q .'
check "every read is a UTC window of at most seven days" 'win_ok "$FAKE/sacct_calls.txt"'
python3 "$STAGE/slurm/budget.py" --site test --json >/dev/null 2>&1; N2=$(wc -l < "$FAKE/sacct_calls.txt")
check "a second check reads only the open windows: the finished ones come from the cache" '[ $((N2-N1)) -ge 1 ] && [ $((N2-N1)) -le 2 ]'
python3 "$STAGE/slurm/budget.py" --site test --json --no-cache >/dev/null 2>&1; N3=$(wc -l < "$FAKE/sacct_calls.txt")
check "--no-cache reads every window again" '[ $((N3-N2)) = "$N1" ]'
rm -f "$STAGE"/receipts/.budget_cache.*.json; touch "$FAKE/sacct_fail"; python3 "$STAGE/slurm/budget.py" --site test --lane "${NAME%%-*}" --est-hours 1 >/dev/null 2>&1; RC=$?
check "accounting down: the check refuses (exit 3) and caches nothing" '[ $RC = 3 ] && ! ls "$STAGE"/receipts/.budget_cache.*.json >/dev/null 2>&1'

echo "22. the login-node load check: tests/fleet_guard/test_login_check.sh"
if TMPDIR="$T" bash "$HERE/test_login_check.sh" "$TOOLING/tools/login_check.py" > "$T/lc.out" 2>&1; then ok "login check suite: $(tail -1 "$T/lc.out")"; else bad "login check suite: $(tail -1 "$T/lc.out")"; cat "$T/lc.out"; fi

echo; echo "RESULT: $PASS passed, $FAILS failed"
