#!/usr/bin/env bash
# lanegit.sh: git for many agents in ONE shared checkout. Each agent (a "lane") owns one folder; this tool keeps every lane's git
# use short under the shared index lock, scoped to its own folder, and pushes without touching the working tree.
#
#   lanegit.sh status <lane-folder>                               scoped status; takes no lock
#   lanegit.sh commit <lane-folder> -m "<message>" -- <paths...>  explicit paths under the lane folder only
#   lanegit.sh push   <lane-name> [<commit>]                      merge-tree route; verifies the commit is on origin; no merge, no ff
#   lanegit.sh check                                              lock holder, staged entries by lane, unpushed count
#   lanegit.sh index  <lane-folder> -- <paths...>                 bring the shared index up to date for paths (after INDEX PENDING)
#
# Environment: LANEGIT_ROOT (the checkout; default: the one you are in), LANEGIT_LANES_PREFIX (the folder the lane folders live in,
# for `check`, e.g. "lanes"), FLEET_GIT_TOKEN_FILE (optional token file for HTTPS, read only by git's credential helper, never printed),
# FLEET_MAX_NEW_FILE_KB (256), FLEET_MAX_FILE_MB (10), FLEET_ALLOW_LARGE=1 (a deliberate exception, to be recorded in the commit
# message), LANEGIT_INDEX_WAIT (seconds to wait for a busy lock before deferring, 120).
# A commit: HEAD's tree plus the named paths are built in a PRIVATE index (read-tree, update-index, write-tree, commit-tree), HEAD moves
# with update-ref guarded by the HEAD it was built on, and only then is the shared index updated for those paths, briefly. Nothing else
# is stat'ed: on a parallel file system a plain partial commit refreshes the whole index under the lock (minutes on a 250,000-file
# tree). If the lock stays busy, the paths are recorded and replayed by the lane's next status, commit or index call.
# A push: a merge commit built with merge-tree + commit-tree (no index, no working tree); local main is never fast-forwarded here
# (git/ffscoped.py levels it when an operator decides to). No git merge or git pull. The lock is waited for, never removed.
# Several login nodes may share the checkout over a POSIX-consistent file system (GPFS): lock files and atomic renames behave the same
# from every node; the private index carries the host name; `check` lists this node's git processes only, so a lock is never judged
# stale from one node. Auto-gc is off for every command.
set -u
ROOT=${LANEGIT_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null)}   # the shared checkout: LANEGIT_ROOT, else the one you are in
TOKEN=${FLEET_GIT_TOKEN_FILE:-}   # optional: a token file for HTTPS pushes, read only by git's credential helper
PWD_ORIG=$PWD
export GIT_OPTIONAL_LOCKS=0
G="git -c core.preloadIndex=false -c gc.auto=0 -c maintenance.auto=false"
CREDOPT=()   # with a token file, one extra credential helper for this tool's fetch and push; otherwise your own git setup applies
if [ -n "$TOKEN" ]; then CREDOPT=(-c "credential.helper=!f() { echo username=x-access-token; echo \"password=\$(cat '$TOKEN')\"; }; f"); fi
die() { echo "lanegit: $*" >&2; exit 1; }
filt() { grep -v -i 'password\|ghp_\|github_pat\|vulnerabilit\|dependabot\|^remote: *$' || true; }
[ -n "$ROOT" ] || die "not inside a git checkout: run it from the shared checkout or set LANEGIT_ROOT"
cd "$ROOT" || die "cannot enter $ROOT"
rel() {  # absolute or cwd-relative path -> repo-relative, inside the checkout
  local p="$1"; case "$p" in /*) ;; *) p="$PWD_ORIG/$p";; esac
  p=$(python3 -c 'import os,sys; print(os.path.normpath(sys.argv[1]))' "$p")
  case "$p" in "$ROOT"/*) printf '%s\n' "${p#$ROOT/}";; *) die "path outside the checkout: $1";; esac
}
GITDIR=$($G rev-parse --git-dir)
wait_lock() {  # waits up to $1 seconds (default 1200) for index.lock to clear; returns 1 on timeout, never removes it
  local max=${1:-1200} i=0
  while [ -e "$GITDIR/index.lock" ]; do
    i=$((i+5)); [ $i -gt "$max" ] && return 1
    sleep 5
  done
  return 0
}
lock_holders() { echo "on $(hostname -s): $(pgrep -a -x git | cut -c1-80 | head -3 | tr '\n' ';')(a holder on another login node shows no process here)"; }
PENDING=""   # set by cmd_* to $GITDIR/lanegit-pending-<lane>.paths: paths whose shared-index update was deferred
committed_info() {  # --index-info lines that set the named paths to HEAD's committed versions; a path HEAD lacks is removed. The shared
  local have p z     # index must hold what was committed: hashing the working files instead staged any edit made since the commit (a
                    # deferred update replayed later did), and a folder commit then passed it over as unchanged
  z=$(printf '%0*d' "$($G rev-parse HEAD | tr -d '\n' | wc -c)" 0)
  have=$'\n'$(GIT_LITERAL_PATHSPECS=1 $G ls-tree -r --full-tree --name-only -z HEAD -- "$@" | tr '\0' '\n')$'\n'
  GIT_LITERAL_PATHSPECS=1 $G ls-tree -r --full-tree -z HEAD -- "$@"
  for p in "$@"; do case "$have" in *$'\n'"$p"$'\n'*) ;; *) printf '0 %s\t%s\0' "$z" "$p";; esac; done
}
index_update() {  # the shared index: the named paths set to their committed versions (HEAD), under the lock, briefly;
  local try=0 out  # on a busy lock the paths are recorded as pending and replayed by the lane's next status, commit or index
  while :; do
    try=$((try+1))
    if ! wait_lock "${LANEGIT_INDEX_WAIT:-120}"; then
      [ -n "$PENDING" ] && printf '%s\n' "$@" >> "$PENDING"
      echo "INDEX PENDING: index.lock held $(( ${LANEGIT_INDEX_WAIT:-120} / 60 )) minutes by another process ($(lock_holders)); the commit stands; the paths are replayed by your next status, commit or index call"; return 1
    fi
    if out=$(committed_info "$@" | $G update-index -z --index-info 2>&1); then return 0; fi
    if printf '%s' "$out" | grep -q 'index.lock\|Unable to create'; then
      [ $try -ge 6 ] && { [ -n "$PENDING" ] && printf '%s\n' "$@" >> "$PENDING"; echo "INDEX PENDING: lock contention six times; the commit stands; the paths are replayed by your next status, commit or index call"; return 1; }
      sleep 5; continue
    fi
    echo "INDEX NOT UPDATED: $out"; return 1
  done
}
replay_pending() {  # $1 = lane rel: a deferred index update replayed once, without waiting, if the lock is free now
  PENDING="$GITDIR/lanegit-pending-$(printf '%s' "$1" | tr '/' '_').paths"
  [ -s "$PENDING" ] || return 0
  [ -e "$GITDIR/index.lock" ] && return 0
  local paths=() q
  while IFS= read -r q; do [ -n "$q" ] && paths+=("$q"); done < <(sort -u "$PENDING")
  if committed_info "${paths[@]}" | $G update-index -z --index-info >/dev/null 2>&1; then rm -f "$PENDING"; echo "INDEX UPDATED (deferred): ${#paths[@]} path(s)"; fi
}
cmd_status() {
  [ $# -ge 1 ] || die 'usage: status <lane-folder>'
  local r; r=$(rel "$1") || exit 1; [ -d "$ROOT/$r" ] || die "no such folder: $1"
  replay_pending "$r"
  $G status --short -- "$r"
}
cmd_commit() {
  [ $# -ge 4 ] || die 'usage: commit <lane-folder> -m "<message>" -- <paths...>'
  local lane="$1"; shift; [ "$1" = "-m" ] || die 'usage: commit <lane-folder> -m "<message>" -- <paths...>'
  local msg="$2"; shift 2; [ "${1:-}" = "--" ] || die 'the paths come after --'; shift
  [ $# -ge 1 ] || die 'no paths given'; [ -n "$msg" ] || die 'empty message'
  local lanerel; lanerel=$(rel "$lane") || exit 1; [ -d "$ROOT/$lanerel" ] || die "no such lane folder: $lane"
  replay_pending "$lanerel"
  local files=() p r f sz
  for p in "$@"; do
    r=$(rel "$p") || exit 1
    case "$r/" in "$lanerel"/*) ;; *) die "path outside your lane folder ($lanerel): $p";; esac
    if [ -d "$ROOT/$r" ]; then
      while IFS= read -r -d '' f; do files+=("$f"); done < <($G ls-files -z -m -o -d --exclude-standard -- "$r"; $G diff-index -z --cached --name-only HEAD -- "$r")   # and what is staged against HEAD
    elif [ -e "$ROOT/$r" ] || $G ls-files --error-unmatch -- "$r" >/dev/null 2>&1; then files+=("$r")
    else die "no such path: $p"; fi
  done
  [ ${#files[@]} -ge 1 ] || die 'nothing changed under the given paths'
  mapfile -d '' files < <(printf '%s\0' "${files[@]}" | sort -zu)   # a path both changed and staged, once
  local limit=$(( ${FLEET_MAX_FILE_MB:-10} * 1024 * 1024 )) tracked=$'\n' untracked=() ign
  tracked+=$($G ls-files -z -- "${files[@]}" | tr '\0' '\n')$'\n'      # one index read for all the paths
  for f in "${files[@]}"; do
    if [ -f "$ROOT/$f" ]; then
      sz=$(stat -c %s "$ROOT/$f")
      case "$tracked" in *$'\n'"$f"$'\n'*) ;; *)
        [ "$sz" -gt $(( ${FLEET_MAX_NEW_FILE_KB:-256} * 1024 )) ] && [ "${FLEET_ALLOW_LARGE:-0}" != 1 ] && die "new file over ${FLEET_MAX_NEW_FILE_KB:-256} KB stays out of git: $f ($sz bytes); slim it or ignore it"
        untracked+=("$f");;
      esac
      [ "$sz" -gt "$limit" ] && [ "${FLEET_ALLOW_LARGE:-0}" != 1 ] && die "file over ${FLEET_MAX_FILE_MB:-10} MB refused (the fleet's pre-commit rule): $f ($sz bytes)"
    fi
  done
  if [ ${#untracked[@]} -ge 1 ]; then ign=$(printf '%s\0' "${untracked[@]}" | $G check-ignore -z --stdin | tr '\0' ' '); [ -z "$ign" ] || die "ignored path(s), not added: $ign"; fi
  if [ "${LANEGIT_ROUTE:-private}" = only ]; then   # the old route (git add + commit --only): a full index refresh twice, kept for comparison only
    local try=0 out out2
    while :; do
      try=$((try+1)); wait_lock 1200 || die "index.lock held for 20 minutes; not removed (holders: $(lock_holders)); try again later"
      if out=$($G add -- "${files[@]}" 2>&1) && out2=$($G commit --only -q -m "$msg" -- "${files[@]}" 2>&1); then break; fi
      if printf '%s\n%s' "${out:-}" "${out2:-}" | grep -q 'index.lock\|cannot lock ref\|Unable to create'; then
        [ $try -ge 6 ] && die "lock contention six times; last: ${out:-} ${out2:-}"; sleep 10; continue
      fi
      die "commit failed: ${out:-} ${out2:-}"
    done
    echo "COMMITTED $($G rev-parse --short=10 HEAD) $(date -u +%H:%M:%SZ) ${#files[@]} path(s): $(printf '%s' "$msg" | head -c 90)"; return 0
  fi
  # The private-index route: the commit = HEAD's tree + the named paths; nothing else is read or stat'ed; the shared
  # index is untouched until the brief update at the end; HEAD is advanced only if it is still the HEAD we built on.
  LANEGIT_TMP_INDEX="$GITDIR/lanegit-index.$(hostname -s).$$.$RANDOM"   # the host in the name: several login nodes share this checkout
  local T="$LANEGIT_TMP_INDEX" try=0 head tree c out
  trap 'rm -f "${LANEGIT_TMP_INDEX:-}"' EXIT
  while :; do
    try=$((try+1)); rm -f "$T"
    head=$($G rev-parse HEAD) || die 'no HEAD'
    GIT_INDEX_FILE=$T $G read-tree "$head" || die 'read-tree failed'
    out=$(GIT_INDEX_FILE=$T $G update-index --add --remove -- "${files[@]}" 2>&1) || die "update-index failed: $out"
    tree=$(GIT_INDEX_FILE=$T $G write-tree) || die 'write-tree failed'
    rm -f "$T"
    if [ "$tree" = "$($G rev-parse "$head^{tree}")" ]; then echo "NOTHING TO COMMIT: the named paths equal HEAD ($($G rev-parse --short=10 "$head"))"; index_update "${files[@]}" >/dev/null 2>&1 || true; return 0; fi
    c=$($G commit-tree "$tree" -p "$head" -m "$msg") || die 'commit-tree failed'
    if $G update-ref -m "lanegit commit ($lanerel)" HEAD "$c" "$head" 2>/dev/null; then break; fi
    [ $try -ge 8 ] && die "HEAD moved under us eight times; try again"
    sleep 2
  done
  echo "COMMITTED $($G rev-parse --short=10 "$c") $(date -u +%H:%M:%SZ) ${#files[@]} path(s): $(printf '%s' "$msg" | head -c 90)"
  if index_update "${files[@]}"; then echo "INDEX UPDATED"; fi
}
cmd_index() {
  [ $# -ge 3 ] || die 'usage: index <lane-folder> -- <paths...>'
  local lane="$1"; shift; [ "${1:-}" = "--" ] || die 'the paths come after --'; shift
  local lanerel; lanerel=$(rel "$lane") || exit 1
  local files=() p r q
  PENDING="$GITDIR/lanegit-pending-$(printf '%s' "$lanerel" | tr '/' '_').paths"
  for p in "$@"; do r=$(rel "$p") || exit 1; case "$r/" in "$lanerel"/*) ;; *) die "path outside your lane folder ($lanerel): $p";; esac; files+=("$r"); done
  if [ -s "$PENDING" ]; then while IFS= read -r q; do [ -n "$q" ] && files+=("$q"); done < <(sort -u "$PENDING"); fi
  if index_update "${files[@]}"; then rm -f "$PENDING"; echo "INDEX UPDATED ${#files[@]} path(s)"; fi
}
cmd_push() {
  local lane="${1:-lane}" want="${2:-}" try=0 out tree mc om m
  [ -n "$want" ] || want=$($G rev-parse HEAD)
  $G rev-parse --verify -q "$want^{commit}" >/dev/null || die "no such commit: $want"
  $G ${CREDOPT[@]+"${CREDOPT[@]}"} fetch -q origin main 2>&1 | filt
  if $G merge-base --is-ancestor "$want" origin/main; then echo "ALREADY ON ORIGIN $($G rev-parse --short=10 "$want") (origin/main $($G rev-parse --short=10 origin/main))"; return 0; fi
  $G merge-base --is-ancestor "$want" main || die "commit $want is not on local main"
  while :; do
    try=$((try+1))
    om=$($G rev-parse origin/main) || die "no origin/main"; m=$($G rev-parse main) || die "no main"   # both refs read ONCE per try: a commit landing between merge-tree and commit-tree must not make the merge claim a parent whose content its tree lacks
    if $G merge-base --is-ancestor "$om" "$m"; then
      out=$($G ${CREDOPT[@]+"${CREDOPT[@]}"} push -q origin "$m":main 2>&1 | filt)
    else
      tree=$($G merge-tree --write-tree "$om" "$m") || die "conflict between origin/main and local main; nothing pushed: $tree"
      mc=$($G commit-tree "$tree" -p "$om" -p "$m" -m "merge: $lane onto origin/main") || die 'commit-tree failed'
      out=$($G ${CREDOPT[@]+"${CREDOPT[@]}"} push -q origin "$mc":main 2>&1 | filt)
    fi
    $G ${CREDOPT[@]+"${CREDOPT[@]}"} fetch -q origin main 2>&1 | filt
    if $G merge-base --is-ancestor "$want" origin/main; then
      echo "PUSHED $($G rev-parse --short=10 "$want") on origin $(date -u +%H:%M:%SZ) (origin/main $($G rev-parse --short=10 origin/main))"; break
    fi
    [ $try -ge 4 ] && die "not on origin after four tries; last: ${out:-}"
    sleep 5
  done
  # No fast-forward of local main here: that step is a working-tree update that holds index.lock for its whole
  # scan (minutes on a large tree) and is refused by other lanes' untracked copies. Local main may stay
  # behind origin/main; the next push merges the two again cheaply (the merge base is the last push's parent).
  local behind; behind=$($G rev-list --count main..origin/main 2>/dev/null || echo '?')
  [ "$behind" = "0" ] || echo "note: local main is $behind commit(s) behind origin/main (each lane push lands on origin as a merge commit; others may push from outside); harmless, nothing to do: an operator levels it with git/ffscoped.py"
}
cmd_check() {
  if [ -e "$GITDIR/index.lock" ]; then
    echo "index.lock present since $(stat -c %y "$GITDIR/index.lock" | cut -c12-19) local ($(( $(date +%s) - $(stat -c %Y "$GITDIR/index.lock") )) s old); git processes on THIS node ($(hostname -s)) only; a holder on another login node shows no process here, so never treat a lock as stale from one node:"
    for p in $(pgrep -x git); do echo "  pid $p $(ps -o stat=,etimes=,args= -p "$p" 2>/dev/null | cut -c1-120)"; done
  else echo "no index.lock"; fi
  echo "staged entries by lane:"
  $G diff --cached --name-only | awk -v pre="${LANEGIT_LANES_PREFIX:-}" '{p=$0; if (pre!="" && index(p, pre "/")==1) p=substr(p, length(pre)+2); split(p, a, "/"); n[a[1]]++} END {for (k in n) print "  " n[k], k}' | sort -rn
  echo "local commits not on origin: $($G rev-list --count origin/main..main)"
}
case "${1:-}" in
  status) shift; cmd_status "$@";;
  commit) shift; cmd_commit "$@";;
  push) shift; cmd_push "$@";;
  index) shift; cmd_index "$@";;
  check) cmd_check;;
  *) sed -n '2,/^set -u$/p' "$0" | sed '$d'; exit 2;;
esac
