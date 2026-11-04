#!/bin/bash
# tests/fleet_guard/test_lanegit.sh LANEGIT_SH: lanegit.sh's update of the shared index, on throwaway repositories. A deferred update
# (the index lock was busy) replayed later sets the index to what was committed, never to a newer edit; a folder commit takes changes
# staged against HEAD as well as those in the working tree; a deleted folder named by its own path is committed as removed; and the
# pre-commit size hook measures a renamed file that grew; push (plain, again, behind origin, a conflict), check and index work; and the
# pre-push hook refuses a blob over the limit; and the hook installer points a clone at the hooks. Nothing here touches the real
# checkout or a remote: LANEGIT_ROOT points into the temporary folder.
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
echo "G5. a deleted folder named by its own path (the third review): its tracked files are removed in the commit"
mkdir -p lanes/a/old; printf 'x\n' > lanes/a/old/x.txt; printf 'y\n' > lanes/a/old/y.txt; RC=$(lg commit lanes/a -m c7 -- lanes/a/old)
check "the folder committed first: two files in HEAD" '[ "$RC" = 0 ] && [ "$(git ls-tree -r --name-only HEAD -- lanes/a/old | wc -l)" = 2 ]'
rm -r lanes/a/old; RC=$(lg commit lanes/a -m "remove old" -- lanes/a/old)
check "deleted and named by its path: COMMITTED, both files gone from HEAD, the folder clean (1.3.18: NOTHING TO COMMIT, both left in HEAD)" '[ "$RC" = 0 ] && grep -q "^COMMITTED" "$T/lg.out" && [ -z "$(git ls-tree -r --name-only HEAD -- lanes/a/old)" ] && [ -z "$(git status --porcelain -- lanes/a)" ]'
RC=$(lg commit lanes/a -m c8 -- lanes/a/never-was)
check "a path that never existed is still refused: no such path" '[ "$RC" != 0 ] && grep -q "no such path" "$T/lg.out"'
echo "G6. the pre-commit size hook measures a renamed file that grew (the third review: git saw R066 and the hook passed it)"
HOOK="$(dirname "$LG")/../git-hooks/pre-commit"; git init -q -b main "$T/h"; cp "$HOOK" "$T/h/.git/hooks/pre-commit"; chmod +x "$T/h/.git/hooks/pre-commit"
( cd "$T/h" && head -c 838861 /dev/zero | tr '\0' a > a.bin && git add a.bin && FLEET_MAX_FILE_MB=1 git commit -q -m small ) > "$T/lg.out" 2>&1; RC=$?
check "an 0.8 MiB file under a 1 MiB limit: committed" '[ "$RC" = 0 ]'
( cd "$T/h" && git mv a.bin b.bin && head -c 419430 /dev/zero | tr '\0' b >> b.bin && git add b.bin && FLEET_MAX_FILE_MB=1 git commit -q -m grown ) > "$T/lg.out" 2>&1; RC=$?
check "renamed and grown to 1.2 MiB: refused, under its new name" '[ "$RC" != 0 ] && grep -q "refused: b.bin is 1258291 bytes" "$T/lg.out"'
( cd "$T/h" && git reset -q --hard && head -c 524288 /dev/zero | tr '\0' d > d.bin && git add d.bin && FLEET_MAX_FILE_MB=1 git commit -q -m d \
  && git mv d.bin e.bin && FLEET_MAX_FILE_MB=1 git commit -q -m renamed ) > "$T/lg.out" 2>&1; RC=$?
check "a file of its own renamed without growing: committed" '[ "$RC" = 0 ] && (cd "$T/h" && git cat-file -e HEAD:e.bin)'
echo "G7. lanegit push, every lane's way to publish: plain, again, behind origin (a merge commit), a conflict, commits it cannot push"
H=$(git rev-parse HEAD); RC=$(lg push a "$H")
check "the lane's commits pushed: PUSHED, origin's main holds HEAD" '[ "$RC" = 0 ] && grep -q "^PUSHED $(git rev-parse --short=10 "$H") on origin" "$T/lg.out" && git --git-dir="$T/origin.git" merge-base --is-ancestor "$H" main'
RC=$(lg push a "$H")
check "pushed again: ALREADY ON ORIGIN" '[ "$RC" = 0 ] && grep -q "^ALREADY ON ORIGIN $(git rev-parse --short=10 "$H")" "$T/lg.out"'
git clone -q "$T/origin.git" "$T/out" 2>/dev/null; ( cd "$T/out" && mkdir -p lanes/b && printf 'o\n' > lanes/b/o.txt && git add -A && git commit -q -m outside && git push -q origin main ) >/dev/null 2>&1
printf 'p\n' > lanes/a/p.txt; RC=$(lg commit lanes/a -m c9 -- lanes/a/p.txt); H2=$(git rev-parse HEAD); RC=$(lg push a "$H2")
check "behind origin: a merge commit pushed (two parents) holding both changes; local main and the working tree untouched; the note says main is behind" '[ "$RC" = 0 ] && grep -q "^PUSHED" "$T/lg.out" && [ "$(git --git-dir="$T/origin.git" log -1 --format=%P main | wc -w)" = 2 ] && git --git-dir="$T/origin.git" cat-file -e main:lanes/b/o.txt && git --git-dir="$T/origin.git" cat-file -e main:lanes/a/p.txt && [ "$(git rev-parse HEAD)" = "$H2" ] && [ ! -e lanes/b/o.txt ] && grep -q "behind origin/main" "$T/lg.out"'
( cd "$T/out" && git pull -q origin main && printf 'theirs\n' > lanes/a/p.txt && git commit -q -am theirs && git push -q origin main ) >/dev/null 2>&1
O=$(git --git-dir="$T/origin.git" rev-parse main); printf 'ours\n' > lanes/a/p.txt; RC=$(lg commit lanes/a -m c10 -- lanes/a/p.txt); RC=$(lg push a "$(git rev-parse HEAD)")
check "a conflict with origin: refused, nothing pushed, origin's main unchanged" '[ "$RC" != 0 ] && grep -q "conflict between origin/main and local main; nothing pushed" "$T/lg.out" && [ "$(git --git-dir="$T/origin.git" rev-parse main)" = "$O" ]'
STRAY=$(git commit-tree "$(git rev-parse HEAD^{tree})" -m stray); RC=$(lg push a "$STRAY")
check "a commit that is not on local main: refused" '[ "$RC" != 0 ] && grep -q "is not on local main" "$T/lg.out"'
RC=$(lg push a 0123456789abcdef0123456789abcdef01234567)
check "a commit that does not exist: refused" '[ "$RC" != 0 ] && grep -q "no such commit" "$T/lg.out"'
echo "G8. lanegit check and index"
RC=$(lg check)
check "check: no index.lock, the staged entries by lane, the local commits not on origin as git counts them" '[ "$RC" = 0 ] && grep -q "^no index.lock" "$T/lg.out" && grep -q "^staged entries by lane:" "$T/lg.out" && grep -q "^local commits not on origin: $(git rev-list --count origin/main..main)$" "$T/lg.out"'
touch .git/index.lock; RC=$(lg check); rm -f .git/index.lock
check "check with the index lock held: it says so" '[ "$RC" = 0 ] && grep -q "^index.lock present since" "$T/lg.out"'
printf 'v9\n' > lanes/a/f.txt; touch .git/index.lock; RC=$(lg commit lanes/a -m c11 -- lanes/a/f.txt); rm -f .git/index.lock; RC=$(lg index lanes/a -- lanes/a/f.txt)
check "index: a deferred update replayed on demand; the index level with HEAD for the path" '[ "$RC" = 0 ] && grep -q "^INDEX UPDATED" "$T/lg.out" && [ -z "$(git diff --cached --name-only -- lanes/a/f.txt)" ] && [ "$(git show HEAD:lanes/a/f.txt)" = v9 ]'
echo "G9. the pre-push size hook: a push whose commits carry a blob over the limit is refused"
HOOKP="$(dirname "$LG")/../git-hooks/pre-push"; git init -q --bare -b main "$T/hp.git"; git init -q -b main "$T/hp"; cp "$HOOKP" "$T/hp/.git/hooks/pre-push"; chmod +x "$T/hp/.git/hooks/pre-push"
( cd "$T/hp" && git remote add origin "$T/hp.git" && printf 's\n' > s.txt && git add s.txt && git commit -q -m small && FLEET_MAX_FILE_MB=1 git push -q origin main ) > "$T/lg.out" 2>&1; RC=$?
check "a first push (a new branch on the remote) with small files: accepted" '[ "$RC" = 0 ] && [ "$(git --git-dir="$T/hp.git" rev-parse main)" = "$(cd "$T/hp" && git rev-parse HEAD)" ]'
( cd "$T/hp" && head -c 1258291 /dev/zero | tr '\0' z > big.bin && git add big.bin && git commit -q -m big && FLEET_MAX_FILE_MB=1 git push -q origin main ) > "$T/lg.out" 2>&1; RC=$?
check "a commit carrying a 1.2 MiB blob pushed under a 1 MiB limit: refused, the file named with its size in bytes, the remote unchanged" '[ "$RC" != 0 ] && grep -q "refused: big.bin (1258291 bytes" "$T/lg.out" && [ "$(git --git-dir="$T/hp.git" rev-parse main)" != "$(cd "$T/hp" && git rev-parse HEAD)" ]'
( cd "$T/hp" && FLEET_MAX_FILE_MB=1 FLEET_ALLOW_LARGE=1 git push -q origin main ) > "$T/lg.out" 2>&1; RC=$?
check "the deliberate exception, FLEET_ALLOW_LARGE=1: pushed" '[ "$RC" = 0 ] && [ "$(git --git-dir="$T/hp.git" rev-parse main)" = "$(cd "$T/hp" && git rev-parse HEAD)" ]'
echo "G10. the hook installer: core.hooksPath set to the hooks' folder inside the clone, relative, and the hooks made executable"
INST="$(dirname "$LG")/../git-hooks"; git init -q -b main "$T/hk"; mkdir -p "$T/hk/tools/git-hooks" "$T/nogit"
cp "$INST/pre-commit" "$INST/pre-push" "$INST/install.sh" "$T/hk/tools/git-hooks/"; chmod -x "$T/hk/tools/git-hooks/pre-commit" "$T/hk/tools/git-hooks/pre-push"
( cd "$T/hk/tools" && bash git-hooks/install.sh && bash git-hooks/install.sh ) > "$T/lg.out" 2>&1; RC=$?
check "run twice from a folder inside the clone: exit 0, core.hooksPath = tools/git-hooks, both hooks executable" '[ "$RC" = 0 ] && [ "$(git -C "$T/hk" config core.hooksPath)" = tools/git-hooks ] && [ -x "$T/hk/tools/git-hooks/pre-commit" ] && [ -x "$T/hk/tools/git-hooks/pre-push" ] && grep -q "^core.hooksPath = tools/git-hooks (clone: " "$T/lg.out"'
( cd "$T/hk" && head -c 1258291 /dev/zero | tr '\0' z > big.bin && git add big.bin && FLEET_MAX_FILE_MB=1 git commit -q -m big ) > "$T/lg.out" 2>&1; RC=$?
check "the installed pre-commit hook acts: a 1.2 MiB file under a 1 MiB limit refused, nothing committed" '[ "$RC" != 0 ] && grep -q "refused: big.bin is 1258291 bytes" "$T/lg.out" && ! git -C "$T/hk" rev-parse -q --verify HEAD >/dev/null'
( cd "$T/nogit" && GIT_CEILING_DIRECTORIES="$T" bash "$T/hk/tools/git-hooks/install.sh" ) > "$T/lg.out" 2>&1; RC=$?
check "run from outside any clone: git refuses (not a git repository) and the installer stops" '[ "$RC" != 0 ] && grep -q "not a git repository" "$T/lg.out" && ! grep -q "^core.hooksPath = " "$T/lg.out"'
echo "LANEGIT RESULT: $PASS passed, $FAILS failed"; [ "$FAILS" = 0 ]
