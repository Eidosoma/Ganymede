#!/bin/bash
# Point this clone at the fleet's tracked hooks (pre-commit and pre-push size guards). Run once per clone, from anywhere inside it:
#     bash path/to/ganymede/git-hooks/install.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; TOP="$(git rev-parse --show-toplevel)"
REL="${HERE#$TOP/}"
chmod +x "$HERE"/pre-commit "$HERE"/pre-push
git config core.hooksPath "$REL"
echo "core.hooksPath = $(git config core.hooksPath) (clone: $TOP)"
