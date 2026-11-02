#!/bin/bash
# tests/fleet_guard/test_login_check.sh LOGIN_CHECK_PY: the login-node load check (tools/login_check.py). It must read the node
# without changing anything, always answer GO, REDUCE or WAIT with the matching exit code, and refuse to start what cannot fit.
# Runs on any Linux machine; nothing is written outside a temporary folder that the test removes.
LC="$(readlink -f "${1:-}")"; [ -n "$LC" ] && [ -f "$LC" ] || { echo "usage: test_login_check.sh <login_check.py>"; exit 2; }
T=$(mktemp -d "${TMPDIR:-/tmp}/lc_test.XXXXXX"); trap 'rm -rf "$T"' EXIT
PASS=0; FAILS=0
ok()   { PASS=$((PASS+1)); echo "  pass: $1"; }
bad()  { FAILS=$((FAILS+1)); echo "  FAIL: $1"; }
check(){ if eval "$2"; then ok "$1"; else bad "$1  [$2]"; fi; }
rj(){ python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(eval(sys.argv[2],{"d":d}))' "$1" "$2" 2>/dev/null; }
run(){ (cd "$T" && PYTHONDONTWRITEBYTECODE=1 python3 "$LC" --sample-s 0.2 "$@" > "$T/out.json" 2> "$T/err.txt"); echo $?; }

echo "L1. a small request: a verdict, its exit code, cores to match the workers"
RC=$(run --json --want-cpus 1 --low-cpus 1 --skip-cores '')
check "exit 0, 1 or 2" '[ "$RC" -le 2 ]'
check "the verdict matches the exit code" '[ "$(rj "$T/out.json" "[\"GO\",\"REDUCE\",\"WAIT\"].index(d[\"verdict\"])")" = "$RC" ]'
check "as many cores as workers, none outside the allowed set" '[ "$(rj "$T/out.json" "len(d[\"cores\"])==d[\"workers\"] and d[\"workers\"]<=d[\"cores_allowed\"]")" = True ]'
check "load, available memory and reasons are reported" '[ "$(rj "$T/out.json" "len(d[\"load\"])==3 and d[\"mem_available_gib\"]>0 and isinstance(d[\"reasons\"],list)")" = True ]'

echo "L2. a run that cannot fit in memory: WAIT, exit 2, no cores offered"
RC=$(run --json --want-cpus 1 --low-cpus 1 --skip-cores '' --mem-gib 100000000)
check "exit 2 and WAIT" '[ "$RC" = 2 ] && [ "$(rj "$T/out.json" "d[\"verdict\"]")" = WAIT ]'
check "no cores and no command prefix on WAIT" '[ "$(rj "$T/out.json" "d[\"cores\"]==[] and d[\"prefix\"]==\"\"")" = True ]'

echo "L3. excluding every core leaves nothing to run on: WAIT"
ALL=$(python3 -c 'import os; print(",".join(map(str, sorted(os.sched_getaffinity(0)))))')
RC=$(run --json --want-cpus 1 --low-cpus 1 --skip-cores "$ALL")
check "exit 2 and WAIT" '[ "$RC" = 2 ] && [ "$(rj "$T/out.json" "d[\"verdict\"]")" = WAIT ]'

echo "L4. the text form and the cores-only form"
RC=$(run --want-cpus 1 --low-cpus 1 --skip-cores '')
check "the text form ends with a verdict line or a run-with line" 'tail -2 "$T/out.json" | grep -qE "^(GO|REDUCE|WAIT)|^run with: taskset -c "'
RC=$(run --print-cores --want-cpus 1 --low-cpus 1 --skip-cores '')
check "the cores-only form prints a core list (or nothing on WAIT)" '[ "$RC" = 2 ] || grep -qE "^[0-9][0-9,-]*$" "$T/out.json"'

echo "L5. read-only"
check "nothing written beside it but the test's own output" '[ "$(ls -A "$T" | sort | tr "\n" " ")" = "err.txt out.json " ]'

echo "L6. the login GPU: what you hold already counts (a fake nvidia-smi: 10 GiB of yours on the card, someone else there too)"
# "Someone else" is a PID that cannot exist (above any pid_max): its owner cannot be read, so the tool counts it as another user's,
# whoever runs the suite. (PID 1 was used first; run as root, PID 1 is the caller's own, and the sum read 22 GiB.)
OTHER=99999999; [ ! -e /proc/$OTHER ] || { echo "PID $OTHER exists here; pick another"; exit 2; }
F=$(mktemp -d "${TMPDIR:-/tmp}/lc_gpu.XXXXXX"); mkdir -p "$F/bin"
cat > "$F/bin/nvidia-smi" <<EOF
#!/bin/bash
case "\$*" in
  *--query-gpu=*) echo "0, 12288, 97871, 30";;
  *--query-compute-apps=*) echo "$$, 10240"; echo "$OTHER, 2048";;
esac
EOF
chmod +x "$F/bin/nvidia-smi"
gr(){ PATH="$F/bin:$PATH" python3 "$LC" --gpu-gib "$1" --json 2>/dev/null | python3 -c 'import json,sys; print(" | ".join(json.load(sys.stdin)["reasons"]))'; }
R10=$(gr 10); R4=$(gr 4); rm -rf "${F:?}"
check "10 GiB more with 10 GiB held and others on the card: over the 16 GiB rule, named" 'echo "$R10" | grep -q "you would hold 20.0 GiB (10.0 already, 10.0 more)"'
check "4 GiB more (14 GiB in all): no objection from the GPU rule" '! echo "$R4" | grep -q "you would hold"'

echo; echo "LOGIN CHECK RESULT: $PASS passed, $FAILS failed"
[ "$FAILS" = 0 ]
