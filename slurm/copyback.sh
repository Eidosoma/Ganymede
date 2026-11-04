#!/bin/bash
# copyback.sh <remote-user@host> <remote-root> <local-root> <list-file>
# Pulls exactly the paths in the list (relative to remote-root) with checksums; run from the agent's machine or a login node.
# Keep the list per lane and small: receipts, sealed reports, selected checkpoints. Scratch is purged; the list is what survives.
# The list is read as rsync reads it in a pull: one path a line (a line that starts with # or ; too: rsync passes the list on as it
# is), blank lines skipped, leading slashes dropped; a folder is created but not filled, and "folder/" brings the files directly in it.
# Afterwards local-root/copyback-<UTC>.sha256 holds the checksum of every file the list brought, as it now is locally.
set -euo pipefail
[ $# = 4 ] || { echo "usage: copyback.sh <remote-user@host> <remote-root> <local-root> <list-file>" >&2; exit 2; }
R="$1"; RROOT="$2"; LROOT="$3"; LIST="$4"
[ -f "$LIST" ] || { echo "copyback.sh: no list file: $LIST" >&2; exit 2; }
LIST="$(cd "$(dirname "$LIST")" && pwd)/$(basename "$LIST")"   # the manifest is made from inside local-root
rsync -avz --checksum --files-from="$LIST" "$R:$RROOT/" "$LROOT/"
M="$LROOT/copyback-$(date -u +%Y%m%dT%H%M%SZ).sha256"
(cd "$LROOT" && while IFS= read -r p || [ -n "$p" ]; do
   [ -n "$p" ] || continue
   while [ "${p#/}" != "$p" ]; do p="${p#/}"; done
   if [ -f "$p" ]; then sha256sum -- "$p"
   elif [ "${p%/}" != "$p" ] && [ -d "$p" ]; then find "${p%/}" -maxdepth 1 -type f ! -name 'copyback-*.sha256*' -print0 | sort -z | xargs -0 -r sha256sum --; fi
 done < "$LIST") > "$M.tmp"
mv "$M.tmp" "$M"; echo "copyback: $(wc -l < "$M") file(s) checksummed in $M"
