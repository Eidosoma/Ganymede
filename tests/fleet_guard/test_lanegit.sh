#!/bin/bash
# tests/fleet_guard/test_lanegit.sh LANEGIT_SH: lanegit.sh's update of the shared index, on throwaway repositories. A deferred update
# (the index lock was busy) replayed later sets the index to what was committed, never to a newer edit; a folder commit takes changes
# staged against HEAD as well as those in the working tree. Nothing here touches the real checkout or a remote: LANEGIT_ROOT points
# into the temporary folder.
LG="$(readlink -f "${1:-}")"; [ -n "$LG" ] && [ -f "$LG" ] || { echo "usage: test_lanegit.sh <lanegit.sh>"; exit 2; }
T=$(mktemp -d "${TMPDIR:-/tmp}/lanegit_test.XXXXXX"); trap 'rm -rf "$T"' EXIT
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.invalid GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.invalid
export GIT_CONFIG_NOSYSTEM=1 HOME="$T/home"; mkdir -p "$HOME"
PASS=0; FAILS=0
ok()   { PASS=$((PASS+1)); echo "  pass: $1"; }
bad()  { FAILS=$((FAILS+1)); echo "  FAIL: $1"; }
check(){ if eval "$2"; then ok "$1"; else bad "$1  [$2]"; sed 's/^/      | /' "$T/lg.out"; fi; }
SH="$T/shared"
git init -q --bare -b main "$T/origin.git"; git init -q -b main "$SH"
cd "$SH" || exit 3; git remote add origin "$T/origin.git"
mkdir -p lanes/a; printf 'base\n' > lanes/a/keep.txt; git add -A; git commit -q -m base; git push -q origin main 2>/dev/null
lg(){ (cd "$SH" && LANEGIT_ROOT="$SH" LANEGIT_INDEX_WAIT=1 bash "$LG" "$@" > "$T/lg.out" 2>&1); echo $?; }
echo "G1. a deferred index update, replayed later, sets the index to the committed version, not to a newer edit"
printf 'v1\n' > lanes/a/f.txt; RC=$(lg commit lanes/a -m c1 -- lanes/a/f.txt)
check "a first commit: COMMITTED, INDEX UPDATED" '[ "$RC" = 0 ] && grep -q "^COMMITTED" "$T/lg.out" && grep -q "^INDEX UPDATED" "$T/lg.out"'
printf 'v2\n' > lanes/a/f.txt; touch .git/index.lock; RC=$(lg commit lanes/a -m c2 -- lanes/a/f.txt)
check "with the index lock held: the commit stands, the index update is deferred" '[ "$RC" = 0 ] && grep -q "INDEX PENDING" "$T/lg.out" && [ "$(git show HEAD:lanes/a/f.txt)" = v2 ]'
printf 'v3\n' > lanes/a/f.txt; rm -f .git/index.lock; RC=$(lg status lanes/a)
check "the next status replays it; the newer edit shows as a change in the working tree, not as staged" '[ "$RC" = 0 ] && grep -q "INDEX UPDATED (deferred)" "$T/lg.out" && [ "$(git status --porcelain -- lanes/a/f.txt)" = " M lanes/a/f.txt" ]'
RC=$(lg commit lanes/a -m c3 -- lanes/a)
check "a folder commit then takes the newer edit" '[ "$RC" = 0 ] && grep -q "^COMMITTED" "$T/lg.out" && [ "$(git show HEAD:lanes/a/f.txt)" = v3 ]'
echo "G2. a change staged against HEAD (the working file equals the index) is taken by a folder commit"
printf 'v4\n' > lanes/a/f.txt; git add lanes/a/f.txt; RC=$(lg commit lanes/a -m c4 -- lanes/a)
check "COMMITTED, HEAD holds v4, the folder clean" '[ "$RC" = 0 ] && [ "$(git show HEAD:lanes/a/f.txt)" = v4 ] && [ -z "$(git status --porcelain -- lanes/a)" ]'
echo "G3. a staged deletion is taken by a folder commit"
git rm -q lanes/a/keep.txt; RC=$(lg commit lanes/a -m c5 -- lanes/a)
check "COMMITTED, keep.txt gone from HEAD, the folder clean" '[ "$RC" = 0 ] && ! git cat-file -e HEAD:lanes/a/keep.txt 2>/dev/null && [ -z "$(git status --porcelain -- lanes/a)" ]'
echo "G4. an ordinary commit still leaves the index level with HEAD for its paths"
printf 'v5\n' > lanes/a/f.txt; printf 'n\n' > lanes/a/new.txt; RC=$(lg commit lanes/a -m c6 -- lanes/a)
check "COMMITTED both, INDEX UPDATED, the folder clean" '[ "$RC" = 0 ] && grep -q "^INDEX UPDATED" "$T/lg.out" && [ "$(git show HEAD:lanes/a/new.txt)" = n ] && [ -z "$(git status --porcelain -- lanes/a)" ]'
echo "LANEGIT RESULT: $PASS passed, $FAILS failed"; [ "$FAILS" = 0 ]
