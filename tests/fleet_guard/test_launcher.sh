#!/bin/bash
# tests/fleet_guard/test_launcher.sh PACK_NODE_PY: the launcher's task shell under the forwarded checkpoint signal (a single command, a
# compound one, a program started with & that saves at once, and a program that does not handle the signal; a CPU pool under the
# signal), and a task that cannot be started (a missing folder, refused before
# anything starts; a start that fails after others have started). Fake mode: nothing here touches a GPU, Slurm or a live receipt.
PN="$(readlink -f "${1:-}")"; [ -n "$PN" ] && [ -f "$PN" ] || { echo "usage: test_launcher.sh <pack_node.py>"; exit 2; }
T=$(mktemp -d "${TMPDIR:-/tmp}/launcher_test.XXXXXX")
cleanup(){ for f in "$T"/*.pid; do [ -s "$f" ] && kill "$(cat "$f")" 2>/dev/null; done; rm -rf "$T"; }
trap cleanup EXIT
PASS=0; FAILS=0
ok()   { PASS=$((PASS+1)); echo "  pass: $1"; }
bad()  { FAILS=$((FAILS+1)); echo "  FAIL: $1"; }
check(){ if eval "$2"; then ok "$1"; else bad "$1  [$2]"; fi; }
rj(){ python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); print(eval(sys.argv[2],{"r":r}))' "$1" "$2" 2>/dev/null; }
cat > "$T/ckpt.py" <<'EOF'
# a checkpoint-aware program: on SIGUSR1 it saves (two seconds), writes its mark, and exits 0
import os, signal, sys, time
def save(*_):
    time.sleep(2)
    open(os.environ['MARK'], 'w').write('saved')
    sys.exit(0)
signal.signal(signal.SIGUSR1, save)
open(os.environ['READY'], 'w').write('ready')
time.sleep(60)
EOF
cat > "$T/fg.py" <<'EOF'
# a program that does not handle SIGUSR1: once running (after $WAIT_FOR exists, if set) it writes $READY, then sleeps
import os, time
w = os.environ.get('WAIT_FOR')
for _ in range(400):
    if not w or os.path.exists(w): break
    time.sleep(0.05)
open(os.environ['READY'], 'w').write('ready')
time.sleep(30)
EOF
spec(){ python3 - "$T/$1.json" "${@:2}" <<'EOF'
import json, sys
out, tasks = sys.argv[1], sys.argv[2:]
gpu = [dict(name='t%d' % i, cmd=c.split('|', 1)[1], cwd=c.split('|', 1)[0]) for i, c in enumerate(tasks)]
json.dump(dict(job='launcher-test', gpu_tasks=gpu, cpu_tasks=[]), open(out, 'w'))
EOF
}
usr1run(){  # $1: name. Starts the launcher on $T/$1.json, sends SIGUSR1 once the task is ready, waits; notes whether the save had finished at its exit
  rm -f "$T/$1.ready" "$T/$1.mark"
  (cd "$T" && READY="$T/$1.ready" MARK="$T/$1.mark" exec python3 "$PN" --spec "$T/$1.json" --receipt-dir "$T/$1" --mode fake --fake-gpus 1 --reserve-cores 0 --grace 5 >"$T/$1.out" 2>"$T/$1.err") &
  local lp=$! i=0
  while [ ! -e "$T/$1.ready" ] && [ $i -lt 150 ]; do sleep 0.1; i=$((i+1)); done
  local t0; t0=$(date +%s.%N); kill -USR1 "$lp"; wait "$lp"; RC=$?
  WAITED=$(python3 -c "import sys; print(round($(date +%s.%N)-$t0,2))")
  SAVED_AT_EXIT=no; [ -e "$T/$1.mark" ] && SAVED_AT_EXIT=yes
}
echo "K1. a compound command (program; echo post) under the forwarded SIGUSR1: the launcher waits for the save"
spec c1 "$T|python3 $T/ckpt.py; echo post"; usr1run c1
check "exit 0; the save had finished when the launcher exited; it waited about two seconds" '[ "$RC" = 0 ] && [ "$SAVED_AT_EXIT" = yes ] && python3 -c "import sys; sys.exit(0 if $WAITED >= 1.5 else 1)"'
check "receipt: the task exited 0, a checkpoint segment, all ok" '[ "$(rj "$T/c1/receipt.json" "r[\"tasks\"][0][\"exit\"]==0 and r[\"checkpoint_segment\"] is True and r[\"all_ok\"] is True")" = True ]'
check "nothing after the program ran: no post" '! grep -q post "$T/c1/t0.out"'
echo "K2. a single command, the shape that always worked: still a checkpoint segment"
spec c2 "$T|python3 $T/ckpt.py"; usr1run c2
check "exit 0, saved, a checkpoint segment" '[ "$RC" = 0 ] && [ "$SAVED_AT_EXIT" = yes ] && [ "$(rj "$T/c2/receipt.json" "r[\"checkpoint_segment\"] is True")" = True ]'
echo "K3. a program that does not handle SIGUSR1, in a compound command: it dies of the signal, nothing more runs, not a checkpoint"
spec c3 "$T|python3 $T/fg.py; echo post"; usr1run c3
check "exit 1, promptly; the task's exit is 138 (128 + SIGUSR1); not a checkpoint segment; no post" '[ "$RC" = 1 ] && python3 -c "import sys; sys.exit(0 if $WAITED < 5 else 1)" && [ "$(rj "$T/c3/receipt.json" "r[\"tasks\"][0][\"exit\"]==138 and r[\"checkpoint_segment\"] is False")" = True ] && ! grep -q post "$T/c3/t0.out"'
echo "K4. a task whose folder does not exist: refused before anything starts"
spec l1 "$T|echo \$\$ > $T/l1.pid; exec sleep 30" "$T/no-such-folder|sleep 1"
(cd "$T" && timeout 20 python3 "$PN" --spec "$T/l1.json" --receipt-dir "$T/l1" --mode fake --fake-gpus 2 --reserve-cores 0 --grace 2 >"$T/l1.out" 2>"$T/l1.err"); RC=$?
check "exit 2 (refused), the folder named, the first task never started" '[ "$RC" = 2 ] && grep -q "REFUSED: task folder(s) missing: t1" "$T/l1.err" && [ ! -e "$T/l1.pid" ]'
echo "K5. a start that fails after another task has started: the started task is stopped, a receipt records the failure"
spec l2 "$T|sleep 30" "$T|sleep 30"
(cd "$T" && PACK_TEST_FAIL_LAUNCH=t1 timeout 20 python3 "$PN" --spec "$T/l2.json" --receipt-dir "$T/l2" --mode fake --fake-gpus 2 --reserve-cores 0 --grace 2 >"$T/l2.out" 2>"$T/l2.err"); RC=$?
check "exit 1 within the grace, a LAUNCH FAILED line" '[ "$RC" = 1 ] && grep -q "LAUNCH FAILED: task t1" "$T/l2.err"'
check "receipt: launch_error names t1 (not started), all_ok false, the started task recorded with a non-zero exit" '[ "$(rj "$T/l2/receipt.json" "r[\"launch_error\"][\"task\"]==\"t1\" and r[\"launch_error\"][\"not_started\"]==[\"t1\"] and r[\"all_ok\"] is False and len(r[\"tasks\"])==1 and r[\"tasks\"][0][\"exit\"]!=0")" = True ]'
P=$(rj "$T/l2/receipt.json" "r[\"tasks\"][0][\"pid\"]")
check "nothing of the started task is left (its process group is empty)" '[ -n "$P" ] && ! pgrep -g "$P" >/dev/null'
echo "K6. a program started with & and waited for (program & wait \$!): the interrupted wait is waited again, the save counts"
spec c6 "$T|python3 $T/ckpt.py & wait \$!"; usr1run c6
check "exit 0, saved, the task's exit 0, a checkpoint segment" '[ "$RC" = 0 ] && [ "$SAVED_AT_EXIT" = yes ] && [ "$(rj "$T/c6/receipt.json" "r[\"tasks\"][0][\"exit\"]==0 and r[\"checkpoint_segment\"] is True")" = True ]'
echo "K7. the same form with a program that does not handle the signal: it dies of it, exit 138, not a checkpoint"
spec c7 "$T|python3 $T/fg.py & wait \$!"; usr1run c7
check "exit 1, the task's exit 138, not a checkpoint segment" '[ "$RC" = 1 ] && [ "$(rj "$T/c7/receipt.json" "r[\"tasks\"][0][\"exit\"]==138 and r[\"checkpoint_segment\"] is False")" = True ]'
echo "K8. a background program that saves beside a foreground one that dies of the signal: the failure is not masked"
spec c8 "$T|READY=\$READY.bg python3 $T/ckpt.py & WAIT_FOR=\$READY.bg python3 $T/fg.py"; usr1run c8
check "exit 1, the task's exit 138, the background save still waited for" '[ "$RC" = 1 ] && [ "$SAVED_AT_EXIT" = yes ] && [ "$(rj "$T/c8/receipt.json" "r[\"tasks\"][0][\"exit\"]==138")" = True ]'
cat > "$T/ckpt0.py" <<'PYEOF'
# a checkpoint-aware program that saves at once: on SIGUSR1 it writes its mark (when $MARK is set) and exits with $RC_ON_SAVE (0 when unset)
import os, signal, time
def save(*_):
    if os.environ.get('MARK'): open(os.environ['MARK'], 'w').write('saved')
    os._exit(int(os.environ.get('RC_ON_SAVE', '0')))
signal.signal(signal.SIGUSR1, save)
open(os.environ['READY'], 'w').write('ready')
time.sleep(60)
PYEOF
echo "K9. a program started with & and waited for that saves at once (the third review's case), ten runs each: its own status every time"
N=0; for i in 1 2 3 4 5 6 7 8 9 10; do spec c9 "$T|python3 $T/ckpt0.py & wait \$!"; usr1run c9; [ "$RC" = 0 ] && [ "$SAVED_AT_EXIT" = yes ] && [ "$(rj "$T/c9/receipt.json" "r[\"tasks\"][0][\"exit\"]==0 and r[\"checkpoint_segment\"] is True and r[\"all_ok\"] is True")" = True ] && N=$((N+1)); done
check "ten of ten: exit 0, saved, the task's exit 0, a checkpoint segment (1.3.17 read 138 in about a third of such runs)" '[ $N = 10 ]'
N=0; for i in 1 2 3 4 5 6 7 8 9 10; do spec c10 "$T|RC_ON_SAVE=3 python3 $T/ckpt0.py & wait \$!"; usr1run c10; [ "$RC" = 1 ] && [ "$(rj "$T/c10/receipt.json" "r[\"tasks\"][0][\"exit\"]==3")" = True ] && N=$((N+1)); done
check "ten of ten for a program that exits 3 at once: the task's exit is its own 3, never the interrupted wait's 138" '[ $N = 10 ]'
python3 - "$PN" "$T" > "$T/k9shell.out" 2>&1 <<'PYEOF'
# the task shell itself, as the launcher builds it, signalled the way the launcher signals a task (its whole process group)
import importlib.util, os, signal, subprocess, sys, time
pn, T = sys.argv[1:3]
s = importlib.util.spec_from_file_location('pn', pn); m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
codes = []
for r in range(20):
    ready = os.path.join(T, f'k9s.{r}.ready'); env = dict(os.environ, READY=ready, MARK='')   # no mark: it exits the moment it is signalled
    p = subprocess.Popen(['bash', '-c', m.TASK_SHELL + f'python3 {T}/ckpt0.py & wait $!'], env=env, start_new_session=True)
    t0 = time.time()
    while not os.path.exists(ready) and time.time() - t0 < 10: time.sleep(0.005)
    os.killpg(p.pid, signal.SIGUSR1)
    try: codes.append(p.wait(timeout=10))
    except subprocess.TimeoutExpired: os.killpg(p.pid, signal.SIGKILL); p.wait(); codes.append('hung')
print(' '.join(map(str, codes)))
PYEOF
check "the task shell itself, signalled as the launcher signals it, twenty runs: exit 0 every time (1.3.17's trap kept 138 in about a third)" '[ "$(tr " " "\n" < "$T/k9shell.out" | sort -u | tr -d "\n")" = 0 ]'
pspec(){ python3 - "$T/$1.json" "$T" "$2" <<'PYEOF'
import json, sys
out, cwd, cmdf = sys.argv[1:4]
json.dump(dict(job='launcher-pool-test', cpu_pools=[dict(name='p', cmd_file=cmdf, threads=1, parallel=1, cwd=cwd)]), open(out, 'w'))
PYEOF
}
poolrun(){  # $1: name. The pool spec $T/$1.json, in cpu mode; SIGUSR1 to the launcher once $T/$1.ready exists
  rm -rf "$T/$1.ready" "$T/$1.mark" "$T/$1"
  (cd "$T" && exec python3 "$PN" --spec "$T/$1.json" --receipt-dir "$T/$1" --mode cpu --reserve-cores 0 --grace 5 >"$T/$1.out" 2>"$T/$1.err") &
  local lp=$! i=0
  while [ ! -e "$T/$1.ready" ] && [ $i -lt 150 ]; do sleep 0.1; i=$((i+1)); done
  local t0; t0=$(date +%s.%N); kill -USR1 "$lp"; wait "$lp"; RC=$?
  WAITED=$(python3 -c "import sys; print(round($(date +%s.%N)-$t0,2))")
  SAVED_AT_EXIT=no; [ -e "$T/$1.mark" ] && SAVED_AT_EXIT=yes
}
echo "K10. a CPU pool under SIGUSR1 (the third review's case): the worker starts nothing more, waits for its command's save, keeps the rest"
printf '%s\n' "READY=$T/p1.ready MARK=$T/p1.mark exec python3 $T/ckpt.py" "echo one > $T/p1.one" "echo two > $T/p1.two" > "$T/p1.cmds"; pspec p1 p1.cmds; poolrun p1
check "exit 0 once the two-second save had finished (1.3.17 returned at once: the worker died of the signal)" '[ "$RC" = 0 ] && [ "$SAVED_AT_EXIT" = yes ] && python3 -c "import sys; sys.exit(0 if $WAITED >= 1.5 else 1)"'
check "receipt: a checkpoint segment, all ok; the pool: none completed, none failed, one interrupted (exit 0), two not started" '[ "$(rj "$T/p1/receipt.json" "r[\"checkpoint_segment\"] is True and r[\"all_ok\"] is True and [(p[\"completed\"],p[\"failed\"],p[\"interrupted\"],p[\"interrupted_exits\"],p[\"not_started\"]) for p in r[\"pools\"]]==[(0,0,1,{\"0\":0},2)]")" = True ]'
check "the resume file holds the three commands in order; the two after it never ran" '[ "$(cat "$T/p1/p.resume.cmds")" = "$(cat "$T/p1.cmds")" ] && [ ! -e "$T/p1.one" ] && [ ! -e "$T/p1.two" ]'
echo "K11. a pool command that does not handle the signal, after one that finished: interrupted and kept, still a checkpoint segment"
printf '%s\n' "true" "READY=$T/p2.ready exec python3 $T/fg.py" "echo three > $T/p2.three" > "$T/p2.cmds"; pspec p2 p2.cmds; poolrun p2
check "exit 0; one completed, one interrupted (killed by the signal, exit -10), one not started; the resume file has the last two" '[ "$RC" = 0 ] && [ "$(rj "$T/p2/receipt.json" "r[\"checkpoint_segment\"] is True and [(p[\"completed\"],p[\"interrupted\"],p[\"interrupted_exits\"],p[\"not_started\"]) for p in r[\"pools\"]]==[(1,1,{\"1\":-10},1)]")" = True ] && [ "$(cat "$T/p2/p.resume.cmds")" = "$(sed -n 2,3p "$T/p2.cmds")" ]'
echo "K12. a pool command that failed before the signal: not a checkpoint segment"
printf '%s\n' "false" "READY=$T/p3.ready exec python3 $T/fg.py" > "$T/p3.cmds"; pspec p3 p3.cmds; poolrun p3
check "exit 1, not a checkpoint segment, one failed; the rest still kept in the resume file" '[ "$RC" = 1 ] && [ "$(rj "$T/p3/receipt.json" "r[\"checkpoint_segment\"] is False and r[\"all_ok\"] is False and r[\"pools\"][0][\"failed\"]==1 and r[\"pools\"][0][\"resume_file\"]==\"p.resume.cmds\"")" = True ]'
echo "K13. a pool command gets PACK_RECEIPT_DIR, as tasks do (the third review: KeyError)"
printf '%s\n' "python3 -c \"import os; open(os.environ['PACK_RECEIPT_DIR'] + '/pool-saw-it', 'w').write('ok')\"" > "$T/p4.cmds"; pspec p4 p4.cmds
(cd "$T" && exec python3 "$PN" --spec "$T/p4.json" --receipt-dir "$T/p4" --mode cpu --reserve-cores 0 --grace 5 >"$T/p4.out" 2>"$T/p4.err"); RC=$?
check "exit 0; the command ran with it: its file is in the receipt folder; one completed, none failed" '[ "$RC" = 0 ] && [ -e "$T/p4/pool-saw-it" ] && [ "$(rj "$T/p4/receipt.json" "r[\"pools\"][0][\"failed\"]==0 and r[\"pools\"][0][\"completed\"]==1")" = True ]'
echo "LAUNCHER RESULT: $PASS passed, $FAILS failed"; [ "$FAILS" = 0 ]
