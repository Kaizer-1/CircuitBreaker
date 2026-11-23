#!/usr/bin/env python3
"""Fail unless every file in deploy/envs/<env>/sealed/ is one SealedSecret holding ciphertext only.

Those folders are allowlisted for gitleaks (.gitleaks.toml): a SealedSecret's encryptedData is
ciphertext and looks like a key, which is safe (ADR-0019). The allowlist would also let a PLAIN
Secret through, so this check is its other half: a file there must be exactly one `kind:
SealedSecret` for its own environment, named like the file, with values under
`spec.encryptedData` and nothing under `data` or `stringData` anywhere.

    python3 tools/check_sealed_secrets.py           # check (make lint, CI)
    python3 tools/check_sealed_secrets.py PATH ...  # check these files instead

Standard library only: a line-based reading of the YAML kubeseal writes (the system python has
no YAML library).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAINTEXT = re.compile(r"^\s*(data|stringData):")


def problems_in(path: Path) -> list[str]:
    text = path.read_text()
    lines = [
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]
    found = []
    if any(line.strip() == "---" for line in lines):
        found.append("a YAML document separator: one SealedSecret per file")
    if "kind: SealedSecret" not in lines:
        found.append("not a SealedSecret")
    if "  encryptedData:" not in lines:
        found.append("no spec.encryptedData")
    for line in lines:
        if PLAINTEXT.match(line):
            found.append(f"plaintext field `{line.strip()}`")
    name = next((line.split(": ", 1)[1] for line in lines if line.startswith("  name: ")), None)
    namespace = next(
        (line.split(": ", 1)[1] for line in lines if line.startswith("  namespace: ")), None
    )
    env = path.parent.parent.name
    if name != path.stem:
        found.append(f"metadata.name {name!r} is not the file name {path.stem!r}")
    if namespace != env:
        found.append(f"metadata.namespace {namespace!r} is not the environment {env!r}")
    return found


def main(argv: list[str]) -> int:
    paths = [Path(p) for p in argv] or sorted((ROOT / "deploy" / "envs").glob("*/sealed/*"))
    failures = [(p, problem) for p in paths for problem in problems_in(p)]
    for path, problem in failures:
        print(f"{path}: {problem}", file=sys.stderr)
    print(f"sealed secrets: {len(paths)} files, {len(failures)} problems")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
