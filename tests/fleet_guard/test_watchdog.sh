#!/bin/bash
# tests/fleet_guard/test_watchdog.sh PACK_NODE_PY: the launcher's GPU-idle watchdog in fake mode with
# PACK_FAKE_GPU_UTIL (tests only: one utilisation for every fake slot, or one per slot, x for a slot not read), and in local mode with a
# stand-in GPM sampler and a fake nvidia-smi for readings that differ by slot. Nothing here touches a GPU, Slurm or a live receipt.
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
mkdir -p "$T/bin"
cat > "$T/bin/nvidia-smi" <<'EOF'
#!/bin/bash
# fake nvidia-smi (tests only): the utilisation rows in $FAKE_SMI_ROWS ("index, util, mem" rows separated by ';')
case "$*" in *utilization.gpu*) printf '%s' "${FAKE_SMI_ROWS:-}" | tr ';' '\n'; echo;; esac
exit 0
EOF
chmod +x "$T/bin/nvidia-smi"
cat > "$T/harness.py" <<'EOF'
# the launcher with a stand-in GPM sampler (tests only): FAKE_GPM "0:0,1:x" = slot 0 at 0 %, slot 1 not read; two slots on fake buses
import importlib.util, os, sys
s = importlib.util.spec_from_file_location('pack_node', sys.argv[1]); m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
BUS = {'0': '0000:01:00.0', '1': '0000:02:00.0'}
class Gpm:
    def __init__(self): self.nv = True
    def read(self):
        out = {}
        for item in os.environ.get('FAKE_GPM', '').split(','):
            slot, _, v = item.partition(':')
            if slot in BUS: out[BUS[slot]] = dict(error='no reading') if v == 'x' else dict(graphics_util=float(v))
        return out
m.GpmSampler = Gpm
m.nvidia_gpus = lambda: [('0', '00000000:01:00.0'), ('1', '00000000:02:00.0')]
sys.argv = ['pack_node.py'] + sys.argv[2:]
m.main()
EOF
echo '{"job":"wd-harness","gpu_tasks":[{"name":"g0","cmd":"sleep 5"},{"name":"g1","cmd":"sleep 5"}],"cpu_tasks":[]}' > "$T/five.json"
hrun(){ (cd "$T" && PATH="$T/bin:$PATH" FAKE_GPM="$1" FAKE_SMI_ROWS="$2" timeout 60 python3 "$T/harness.py" "$PN" --spec "$T/five.json" --receipt-dir "$T/$3" --mode local --gpus 0,1 --reserve-cores 0 --grace 1 --sample 0.3 --idle-gpu-minutes 0.03 >"$T/$3.out" 2>"$T/$3.err"); echo $?; }
echo "W6. one slot idle, the other never read (GPM has no reading for it and nvidia-smi does not list it): not idle, the job runs to its end"
RC=$(hrun "0:0,1:x" "0, 0, 100" h6)
check "exit 0, no trigger, the unread samples counted" '[ "$RC" = 0 ] && [ "$(rj "$T/h6/receipt.json" "r[\"gpu_idle_watchdog\"][\"triggered\"] is None and r[\"gpu_idle_watchdog\"][\"unread_samples\"]>0")" = True ]'
echo "W7. GPM misses slot 1 and nvidia-smi reads it idle: every slot read and idle, the watchdog ends the job"
RC=$(hrun "0:0,1:x" "0, 0, 100;1, 0, 100" h7)
check "exit 1, triggered" '[ "$RC" = 1 ] && [ "$(rj "$T/h7/receipt.json" "r[\"gpu_idle_watchdog\"][\"triggered\"] is not None")" = True ]'
echo "W8. GPM misses slot 1 and nvidia-smi reads it busy (50 %): not idle"
RC=$(hrun "0:0,1:x" "0, 0, 100;1, 50, 100" h8)
check "exit 0, no trigger" '[ "$RC" = 0 ] && [ "$(rj "$T/h8/receipt.json" "r[\"gpu_idle_watchdog\"][\"triggered\"] is None")" = True ]'
echo "W9. a NaN is not a reading: slot 1 NaN in GPM and absent from nvidia-smi: not idle"
RC=$(hrun "0:0,1:nan" "0, 0, 100" h9)
check "exit 0, no trigger" '[ "$RC" = 0 ] && [ "$(rj "$T/h9/receipt.json" "r[\"gpu_idle_watchdog\"][\"triggered\"] is None")" = True ]'
echo "W10. fake mode with one value a slot, x for a slot not read (0,x): not idle"
RC=$(run "0,x" "$T/short.json" r10 --sample 0.2 --idle-gpu-minutes 0.02)
check "exit 0, no trigger, the unread samples counted" '[ "$RC" = 0 ] && [ "$(rj "$T/r10/receipt.json" "r[\"gpu_idle_watchdog\"][\"triggered\"] is None and r[\"gpu_idle_watchdog\"][\"unread_samples\"]>0")" = True ]'
echo "WATCHDOG RESULT: $PASS passed, $FAILS failed"; [ "$FAILS" = 0 ]
