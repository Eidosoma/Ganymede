#!/bin/bash
# copyback.sh <remote-user@host> <remote-root> <local-root> <list-file>
# Pulls exactly the paths in the list (relative to remote-root) with checksums; run from the agent's machine or a login node.
# Keep the list per lane and small: receipts, sealed reports, selected checkpoints. Scratch is purged; the list is what survives.
set -euo pipefail
R="$1"; RROOT="$2"; LROOT="$3"; LIST="$4"
rsync -avz --checksum --files-from="$LIST" "$R:$RROOT/" "$LROOT/"
(cd "$LROOT" && find . -type f -newer "$LIST" -print0 | xargs -0 sha256sum) > "$LROOT/copyback-$(date -u +%Y%m%dT%H%M%SZ).sha256"
