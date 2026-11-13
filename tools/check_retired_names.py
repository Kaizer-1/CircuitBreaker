#!/usr/bin/env python3
"""Fail if a retired name appears anywhere outside a dated history note.

The five instruments were renamed on 2026-11-14 to professional, fictional company names
(docs/00-overview.md §3). The old tickers and company names may stay only in dated history notes, fenced
by two marker lines in whatever comment syntax the file uses (a marker starts its line; one mentioned in
the middle of a sentence or in code is just text):

    <!-- retired-names:begin 2026-11-14 -->   (the begin marker must carry an ISO date)
    ... text that may name the old instruments ...
    <!-- retired-names:end -->

Every file Git tracks (or would track) is checked, and so is every file path. The match ignores case and
word boundaries, so identifiers and file names that contain an old name are caught too.

    python3 tools/check_retired_names.py           # check (make lint, CI)
    python3 tools/check_retired_names.py PATH ...  # check other files too (e.g. private notes)
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# retired-names:begin 2026-11-14 (the playful names used before the rename)
RETIRED = ("chaiwala", "jugaad", "dabba", "monsoon", "filmy", "indian-flavoured")
# retired-names:end

PATTERN = re.compile("|".join(re.escape(word) for word in RETIRED), re.IGNORECASE)
TAG = "retired-names"
# A marker line: optional comment syntax (<!--, #, //, /*, --, >), then the marker.
MARKER = re.compile(r"^\s*(?:<!--|#|//|/\*|--|>)?\s*" + TAG + r":(begin|end)\b\s*(\S*)")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}$")


def tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [ROOT / name for name in out.split("\0") if name and (ROOT / name).is_file()]


def problems_in(path: Path, shown: str) -> list[str]:
    found = []
    if PATTERN.search(shown):
        found.append(f"{shown}: retired name in the file path")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return found  # binary file (images, fonts)
    open_at = 0  # line number of the open begin marker, 0 when outside a history note
    for number, line in enumerate(lines, start=1):
        marker = MARKER.match(line)
        if marker and marker.group(1) == "begin":
            if open_at:
                found.append(
                    f"{shown}:{number}: history note opened again (first opened on line {open_at})"
                )
            elif not DATE.match(marker.group(2)):
                found.append(
                    f"{shown}:{number}: history note needs an ISO date: {TAG}:begin YYYY-MM-DD"
                )
            open_at = number
        elif marker:
            if not open_at:
                found.append(f"{shown}:{number}: {TAG}:end without a begin marker")
            open_at = 0
        elif not open_at and PATTERN.search(line):
            found.append(f"{shown}:{number}: {line.strip()[:120]}")
    if open_at:
        found.append(f"{shown}:{open_at}: history note never closed ({TAG}:end)")
    return found


def main(extra: list[str]) -> int:
    files = [(path, path.relative_to(ROOT).as_posix()) for path in tracked_files()]
    files += [(Path(name).resolve(), name) for name in extra]
    problems = [problem for path, shown in files for problem in problems_in(path, shown)]
    for problem in problems:
        print(problem)
    if problems:
        print(
            f"\n{len(problems)} place(s) still use a retired instrument name. Use the current tickers "
            f"(docs/00-overview.md §3), or fence a dated history note with {TAG}:begin YYYY-MM-DD "
            f"and {TAG}:end."
        )
        return 1
    print(f"retired names: {len(files)} files checked, none outside dated history notes")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
