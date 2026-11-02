#!/bin/bash
# tests/fleet_guard/test_ffscoped.sh FFSCOPED_PY: the scoped fast-forward (git/ffscoped.py) on
# throwaway repositories: a bare "origin", a "shared" checkout cloned at the base, an "outside" clone that pushes ahead (adds,
# edits, a deletion that empties a folder, a mode change, a retargeted and a new symlink, a path with spaces, a non-ASCII name).
# Nothing here touches the real checkout or GitHub: FF_ROOT always points into the temp folder and the remote is a local path.
FF="$(readlink -f "${1:-}")"; [ -n "$FF" ] && [ -f "$FF" ] || { echo "usage: test_ffscoped.sh <ffscoped.py>"; exit 2; }
T=$(mktemp -d "${TMPDIR:-/tmp}/ff_test.XXXXXX"); trap 'rm -rf "$T"' EXIT
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.invalid GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.invalid
export GIT_CONFIG_NOSYSTEM=1 HOME="$T/home"; mkdir -p "$HOME"
PASS=0; FAILS=0
ok()   { PASS=$((PASS+1)); echo "  pass: $1"; }
bad()  { FAILS=$((FAILS+1)); echo "  FAIL: $1"; }
check(){ if eval "$2"; then ok "$1"; else bad "$1  [$2]"; sed 's/^/      | /' "$T/ff.out"; fi; }
W="$T/w"; SH="$W/shared"
mk() {
  rm -rf "$W"; mkdir -p "$W"; cd "$W" || exit 3
  git init -q --bare -b main origin.git
  git clone -q origin.git seed 2>/dev/null; cd seed
  printf 'a1\n' > a.txt; mkdir -p dir lane; printf 'b1\n' > dir/b.txt; printf '#!/bin/sh\necho hi\n' > run.sh; chmod 755 run.sh
  ln -s a.txt link; printf 'x1\n' > lane/x.txt; printf 'keep\n' > keep.txt
  git add -A; git commit -q -m base; git push -q origin main 2>/dev/null
  cd "$W"; git clone -q origin.git shared 2>/dev/null
  cd seed
  printf 'a2\n' > a.txt; git rm -q dir/b.txt; chmod 644 run.sh; rm link; ln -s keep.txt link; ln -s a.txt link2
  mkdir -p "new dir" deep/er; printf 'c\n' > "new dir/c d.txt"; printf 'u\n' > "ü.txt"; printf 'z\n' > deep/er/z.txt
  git add -A; git commit -q -m outside1; printf 'a3\n' > a.txt; git commit -q -am outside2; git push -q origin main 2>/dev/null
  cd "$W"
}
ff() { (cd "$T" && FF_ROOT="$SH" FF_WAIT_S=1 FF_WAIT_TRIES=2 FF_LOCK_WAIT_S=2 "$@" > "$T/ff.out" 2>&1); echo $?; }
head_is_origin() { [ "$(git -C "$SH" rev-parse HEAD)" = "$(git -C "$W/origin.git" rev-parse main)" ]; }
clean_status() { [ -z "$(git -C "$SH" status --porcelain --untracked-files=all)" ]; }
outside_applied() {
  [ "$(cat "$SH/a.txt")" = a3 ] && [ ! -e "$SH/dir/b.txt" ] && [ ! -d "$SH/dir" ] && [ ! -x "$SH/run.sh" ] &&
  [ "$(readlink "$SH/link")" = keep.txt ] && [ "$(readlink "$SH/link2")" = a.txt ] && [ "$(cat "$SH/new dir/c d.txt")" = c ] &&
  [ "$(cat "$SH/ü.txt")" = u ] && [ "$(cat "$SH/deep/er/z.txt")" = z ]
}
base_untouched() { [ "$(cat "$SH/a.txt")" = a1 ] || [ "$(cat "$SH/a.txt")" = local ]; }
push_lane() {  # the lane's push as lanegit makes it: origin's main merged with the lane's commit, pushed (here via an aux clone)
  (cd "$W" && rm -rf aux && git clone -q origin.git aux 2>/dev/null && cd aux && git fetch -q ../shared main && git merge -q --no-edit FETCH_HEAD && git push -q origin main 2>/dev/null)
}

echo "F1. a clean fast-forward (with GIT_OPTIONAL_LOCKS=0 in the caller's environment, which the tool drops)"
mk; printf 'n\n' > "$SH/lane/notes.txt"; B=$(git -C "$SH" rev-parse HEAD)
RC=$(ff env GIT_OPTIONAL_LOCKS=0 python3 "$FF")
check "exit 0 and DONE" '[ "$RC" = 0 ] && grep -q "DONE: main" "$T/ff.out"'
check "main equals origin's main" 'head_is_origin'
check "every outside change on disk: edit, deletion and its emptied folder, mode, symlinks, spaces, non-ASCII, a new deep folder" 'outside_applied'
check "index and working tree level with main (status shows only the lane's untracked note)" '[ "$(git -C "$SH" status --porcelain --untracked-files=all)" = "?? lane/notes.txt" ]'
check "the lane's untracked file untouched" '[ "$(cat "$SH/lane/notes.txt")" = n ]'
check "reflog records the tool" 'git -C "$SH" reflog -1 --format=%gs | grep -q "ffscoped: main to origin/main"'

echo "F2. the dry run changes nothing"
mk; B=$(git -C "$SH" rev-parse HEAD); RC=$(ff python3 "$FF" --dry-run)
check "exit 0, DRY RUN OK, main and files unchanged, status clean" '[ "$RC" = 0 ] && grep -q "DRY RUN OK" "$T/ff.out" && [ "$(git -C "$SH" rev-parse HEAD)" = "$B" ] && [ "$(cat "$SH/a.txt")" = a1 ] && [ ! -e "$SH/ü.txt" ] && clean_status'

echo "F3. a lane's unstaged edit on a path origin changes: refused, nothing written"
mk; B=$(git -C "$SH" rev-parse HEAD); printf 'local\n' > "$SH/a.txt"; RC=$(ff python3 "$FF")
check "exit 5, the path named, main unchanged, the edit kept, no outside file written" '[ "$RC" = 5 ] && grep -q "edited locally: a.txt" "$T/ff.out" && [ "$(git -C "$SH" rev-parse HEAD)" = "$B" ] && [ "$(cat "$SH/a.txt")" = local ] && [ ! -e "$SH/ü.txt" ] && [ -e "$SH/dir/b.txt" ]'

echo "F4. a lane's staged change on a path origin changes: refused"
mk; B=$(git -C "$SH" rev-parse HEAD); printf 'local\n' > "$SH/a.txt"; git -C "$SH" add a.txt; printf 'a1\n' > "$SH/a.txt"; RC=$(ff python3 "$FF")
check "exit 5, staged locally named, main unchanged, the staged entry kept" '[ "$RC" = 5 ] && grep -q "staged locally: a.txt" "$T/ff.out" && [ "$(git -C "$SH" rev-parse HEAD)" = "$B" ] && [ "$(git -C "$SH" diff --cached --name-only)" = a.txt ]'

echo "F5. an untracked file where origin adds one: refused if different, accepted if identical"
mk; printf 'other\n' > "$SH/ü.txt"; RC=$(ff python3 "$FF")
check "different: exit 5, untracked file in the way, the file kept" '[ "$RC" = 5 ] && grep -q "untracked file in the way: ü.txt" "$T/ff.out" && [ "$(cat "$SH/ü.txt")" = other ]'
mk; printf 'u\n' > "$SH/ü.txt"; RC=$(ff python3 "$FF")
check "identical: exit 0, level and clean" '[ "$RC" = 0 ] && head_is_origin && clean_status && outside_applied'

echo "F6. local main has a commit origin lacks (a lane's commit not pushed): refused after the short wait, nothing changed"
mk; printf 'x2\n' > "$SH/lane/x.txt"; git -C "$SH" commit -q -am lane; B=$(git -C "$SH" rev-parse HEAD); RC=$(ff python3 "$FF")
check "exit 3, waited twice, main unchanged, no outside file written" '[ "$RC" = 3 ] && [ $(grep -c "waiting 1 s for its push" "$T/ff.out") = 2 ] && [ "$(git -C "$SH" rev-parse HEAD)" = "$B" ] && [ ! -e "$SH/ü.txt" ]'
push_lane; RC=$(ff python3 "$FF")
check "after the lane's push: exit 0, level, clean, the lane's commit kept" '[ "$RC" = 0 ] && head_is_origin && clean_status && git -C "$SH" merge-base --is-ancestor "$B" HEAD && [ "$(cat "$SH/lane/x.txt")" = x2 ] && outside_applied'

echo "F7. the race that motivated the tool: a lane commits and pushes after the files are written, before main moves"
mk; HOOK='printf "x2\n" > lane/x.txt && git commit -q -m lane-mid lane/x.txt && git -C . log -1 --format=%H > '"$T"'/lanecommit'"; $(declare -f push_lane); W='$W'; push_lane"
RC=$(ff env FF_TEST_BEFORE_REF="$HOOK" python3 "$FF"); LC=$(cat "$T/lanecommit" 2>/dev/null)
check "the first ref update refused (main moved), a second round finishes: exit 0" '[ "$RC" = 0 ] && grep -q "main moved during round 1" "$T/ff.out" && grep -q "DONE: main" "$T/ff.out"'
check "main equals origin's main, contains the lane's commit, status clean, the lane's file kept, outside applied" 'head_is_origin && git -C "$SH" merge-base --is-ancestor "$LC" HEAD && clean_status && [ "$(cat "$SH/lane/x.txt")" = x2 ] && outside_applied'

echo "F8. the race with no push yet: refused after the wait, files at origin's version; a rerun after the push finishes"
mk; HOOK='printf "x2\n" > lane/x.txt && git commit -q -m lane-mid lane/x.txt'
RC=$(ff env FF_TEST_BEFORE_REF="$HOOK" python3 "$FF")
check "exit 3 with the rerun note" '[ "$RC" = 3 ] && grep -q "a rerun finishes" "$T/ff.out" && outside_applied'
push_lane; RC=$(ff python3 "$FF")
check "rerun: exit 0, level, clean" '[ "$RC" = 0 ] && head_is_origin && clean_status && [ "$(cat "$SH/lane/x.txt")" = x2 ]'

echo "F9. nothing to do; index.lock held"
mk; RC=$(ff python3 "$FF"); RC2=$(ff python3 "$FF")
check "a second run: exit 0, nothing to do" '[ "$RC" = 0 ] && [ "$RC2" = 0 ] && grep -q "nothing to do" "$T/ff.out"'
mk; B=$(git -C "$SH" rev-parse HEAD); touch "$SH/.git/index.lock"; RC=$(ff python3 "$FF"); rm -f "$SH/.git/index.lock"
check "lock held: exit 4, main and files unchanged" '[ "$RC" = 4 ] && [ "$(git -C "$SH" rev-parse HEAD)" = "$B" ] && [ ! -e "$SH/ü.txt" ]'

echo "F10. a lane's staged work on a path origin does not change survives the fast-forward"
mk; printf 'x9\n' > "$SH/lane/x.txt"; git -C "$SH" add lane/x.txt; RC=$(ff python3 "$FF")
check "exit 0, level; lane/x.txt still staged with its content" '[ "$RC" = 0 ] && head_is_origin && outside_applied && [ "$(git -C "$SH" status --porcelain --untracked-files=all)" = "M  lane/x.txt" ] && [ "$(git -C "$SH" show :lane/x.txt)" = x9 ]'

echo; echo "RESULT: $PASS passed, $FAILS failed"
[ "$FAILS" = 0 ]
