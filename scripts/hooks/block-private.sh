#!/usr/bin/env bash
# pre-commit hook: refuse any commit that stages private/ or CLAUDE.md (they are git-ignored on purpose).
set -euo pipefail
staged=$(git diff --cached --name-only --diff-filter=ACMR | grep -E '^(private/|CLAUDE\.md$)' || true)
if [ -n "$staged" ]; then
  echo "ERROR: these files must never be committed (private/ and CLAUDE.md):" >&2
  echo "$staged" | sed 's/^/  /' >&2
  echo "Unstage with: git restore --staged <file>" >&2
  exit 1
fi
