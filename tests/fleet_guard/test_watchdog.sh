#!/bin/bash
# tests/fleet_guard/test_watchdog.sh PACK_NODE_PY: the launcher's GPU-idle watchdog in fake mode with
# PACK_FAKE_GPU_UTIL (tests only: every fake slot samples that utilisation). Nothing here touches a GPU, Slurm or a live receipt.
PN="$(readlink -f "${1:-}")"; [ -n "$PN" ] && [ -f "$PN" ] || { echo "usage: test_watchdog.sh <pack_node.py>"; exit 2; }   # absolute: the tests cd into a temp dir
T=$(mktemp -d "${TMPDIR:-/tmp}/wd_test.XXXXXX"); trap 'rm -rf "$T"' EXIT
PASS=0; FAILS=0
ok()   { PASS=$((PASS+1)); echo "  pass: $1"; }
bad()  { FAILS=$((FAILS+1)); echo "  FAIL: $1"; }
check(){ if eval "$2"; then ok "$1"; else bad "$1  [$2]"; fi; }
rj(){ python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); print(eval(sys.argv[2],{"r":r}))' "$1" "$2" 2>/dev/null; }
echo '{"job":"alpha-wdtest","gpu_tasks":[{"name":"g0","cmd":"sleep 60"},{"name":"g1","cmd":"sleep 60"}],"cpu_tasks":[]}' > "$T/long.json"
echo '{"job":"alpha-wdtest","gpu_tasks":[{"name":"g0","cmd":"sleep 2"},{"name":"g1","cmd":"sleep 2"}],"cpu_tasks":[]}' > "$T/short.json"
run(){ (cd "$T" && PACK_FAKE_GPU_UTIL="$1" timeout 60 python3 "$PN" --spec "$2" --receipt-dir "$T/$3" --mode fake --fake-gpus 2 --reserve-cores 0 --grace 1 "${@:4}" >"$T/$3.out" 2>"$T/$3.err"); echo $?; }
echo "W1. every slot idle (0 %) for the window: the launcher ends the job, the receipt says so, all_ok false"
t0=$(date +%s); RC=$(run 0 "$T/long.json" r1 --sample 0.2 --idle-gpu-minutes 0.02); t1=$(date +%s)
check "exit 1 (a failure, not a checkpoint segment)" '[ "$RC" = 1 ]'
check "ended within 20 s, not at the tasks' 60 s" '[ $((t1-t0)) -lt 20 ]'
check "receipt: gpu_idle_watchdog.triggered set, idle_s >= 1.2" '[ "$(rj "$T/r1/receipt.json" "r[\"gpu_idle_watchdog\"][\"triggered\"] is not None and r[\"gpu_idle_watchdog\"][\"idle_s\"]>=1.2")" = True ]'
check "receipt: interrupted by SIGTERM (15), all_ok false, checkpoint_segment false" '[ "$(rj "$T/r1/receipt.json" "r[\"interrupted\"][\"signal\"]==15 and r[\"all_ok\"] is False and r[\"checkpoint_segment\"] is False")" = True ]'
check "both tasks recorded with a non-zero exit" '[ "$(rj "$T/r1/receipt.json" "len(r[\"tasks\"])==2 and all(t[\"exit\"]!=0 for t in r[\"tasks\"])")" = True ]'
check "receipt.md names the watchdog" 'grep -q "GPU-IDLE WATCHDOG" "$T/r1/receipt.md"'
check "the launcher's stdout (the Slurm job log) carries a timestamped GPU-IDLE WATCHDOG line" 'grep -q "GPU-IDLE WATCHDOG: every GPU slot under" "$T/r1.out"'
check "receipt: idle_since set and last_active_by_slot has every slot" '[ "$(rj "$T/r1/receipt.json" "r[\"gpu_idle_watchdog\"][\"idle_since\"] is not None and sorted(r[\"gpu_idle_watchdog\"][\"last_active_by_slot\"])==[\"fake0\",\"fake1\"]")" = True ]'
echo "W2. slots active (40 %): the job runs to its end, no trigger"
RC=$(run 40 "$T/short.json" r2 --sample 0.2 --idle-gpu-minutes 0.02)
check "exit 0, all_ok true, triggered null" '[ "$RC" = 0 ] && [ "$(rj "$T/r2/receipt.json" "r[\"all_ok\"] is True and r[\"gpu_idle_watchdog\"][\"triggered\"] is None")" = True ]'
echo "W3. --idle-gpu-minutes 0 disables it even when idle"
RC=$(run 0 "$T/short.json" r3 --sample 0.2 --idle-gpu-minutes 0)
check "exit 0, all_ok true, no trigger" '[ "$RC" = 0 ] && [ "$(rj "$T/r3/receipt.json" "r[\"all_ok\"] is True and r[\"gpu_idle_watchdog\"][\"triggered\"] is None and r[\"gpu_idle_watchdog\"][\"minutes\"]==0")" = True ]'
echo "W4. the defaults: 45 minutes at 1 percent, in every receipt"
RC=$(run 40 "$T/short.json" r4 --sample 0.2)
check "receipt carries minutes 45, util_percent 1" '[ "$RC" = 0 ] && [ "$(rj "$T/r4/receipt.json" "r[\"gpu_idle_watchdog\"][\"minutes\"]==45 and r[\"gpu_idle_watchdog\"][\"util_percent\"]==1")" = True ]'
echo "W5. a short idle gap under the window does not trigger (idle 0 % but the window longer than the job)"
RC=$(run 0 "$T/short.json" r5 --sample 0.2 --idle-gpu-minutes 0.5)
check "exit 0, no trigger" '[ "$RC" = 0 ] && [ "$(rj "$T/r5/receipt.json" "r[\"gpu_idle_watchdog\"][\"triggered\"] is None and r[\"all_ok\"] is True")" = True ]'
echo "WATCHDOG RESULT: $PASS passed, $FAILS failed"; [ "$FAILS" = 0 ]
