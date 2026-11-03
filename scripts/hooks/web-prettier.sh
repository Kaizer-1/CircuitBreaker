#!/usr/bin/env bash
# pre-commit hook: run web/'s Prettier on the staged web files (paths arrive relative to the repo root).
set -euo pipefail
[ "$#" -eq 0 ] && exit 0
if [ ! -d web/node_modules ]; then
  echo "web/node_modules missing: run 'make setup' (npm ci in web/) first." >&2
  exit 1
fi
files=()
for f in "$@"; do files+=("${f#web/}"); done
cd web && npx --no-install prettier --check "${files[@]}"
