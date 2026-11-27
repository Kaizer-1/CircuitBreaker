"""Compare two evaluation runs case by case (the owner's gate for any prompt change, 2026-11-27):
the same tool chosen in every run, as consistently as before; and the explanations as concrete as
before (how many numbers they cite) and in plain words (no evidence field names).

    uv run python -m eval.compare build/eval/agent-eval-OLD.json build/eval/agent-eval-NEW.json

Exits 1 if any case's decisions changed.
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Any

from eval.plain import SNAKE, quoted_fields

NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def load(path: str) -> dict[str, dict[str, Any]]:
    return {c["case"]: c for c in json.loads(Path(path).read_text())["cases"]}


def field_names_of(facts: Any) -> set[str]:
    names: set[str] = set()

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for key, value in x.items():
                if SNAKE.match(key):
                    names.add(key)
                walk(value)
        elif isinstance(x, list):
            for value in x:
                walk(value)

    walk(facts)
    return names


def decisions(case: dict[str, Any]) -> Counter[str]:
    return Counter(r["proposal"] for r in case["runs"])


def explanations(case: dict[str, Any]) -> list[str]:
    return [r.get("explanation") or "" for r in case["runs"] if r.get("path") == "model"]


def main(old_path: str, new_path: str) -> int:
    old, new = load(old_path), load(new_path)
    changed = 0
    rows = []
    for name in old:
        if name not in new:
            continue
        before, after = decisions(old[name]), decisions(new[name])
        same = before == after
        changed += not same
        fields = field_names_of(old[name].get("facts")) | field_names_of(new[name].get("facts"))
        texts_b, texts_a = explanations(old[name]), explanations(new[name])
        nums_b = mean(len(NUMBER.findall(t)) for t in texts_b) if texts_b else 0.0
        nums_a = mean(len(NUMBER.findall(t)) for t in texts_a) if texts_a else 0.0
        quoted_b = sum(bool(quoted_fields(t, fields)) for t in texts_b)
        quoted_a = sum(bool(quoted_fields(t, fields)) for t in texts_a)
        rows.append((name, same, before, after, nums_b, nums_a, quoted_b, quoted_a))
    width = max(len(r[0]) for r in rows)
    print(f"{'case':{width}}  decisions        numbers cited  field names quoted")
    for name, same, before, after, nb, na, qb, qa in rows:
        verdict = "same" if same else "CHANGED"
        print(f"{name:{width}}  {verdict:8}         {nb:4.1f} -> {na:4.1f}    {qb} -> {qa}")
        if not same:
            print(f"{'':{width}}    before {dict(before)}\n{'':{width}}    after  {dict(after)}")
    all_b = [t for c in old.values() for t in explanations(c)]
    all_a = [t for c in new.values() for t in explanations(c)]
    nums = [mean(len(NUMBER.findall(t)) for t in texts) for texts in (all_b, all_a)]
    words = [mean(len(t.split()) for t in texts) for texts in (all_b, all_a)]
    print(
        f"\nnumbers cited per explanation: {nums[0]:.2f} -> {nums[1]:.2f};"
        f" words: {words[0]:.0f} -> {words[1]:.0f}"
    )
    print(f"cases whose decisions changed: {changed}")
    return 1 if changed else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
