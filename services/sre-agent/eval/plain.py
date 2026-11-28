"""The owner's plain-words check (2026-11-27): an explanation is read aloud from a projector, so it
names the evidence in everyday words ("all of the gateway's errors come from order-service"),
never by the evidence's own field names ("passed_on_share 1.0"). Cheap, no model: every
lower-case snake_case key of the evidence the model was shown, looked for as a whole word. Error
codes such as INTERNAL_ERROR are the system's own vocabulary (the dashboard shows them), not
field names."""

import json
import re
from typing import Any

SNAKE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")


def field_names(evidence_json: str) -> set[str]:
    """Every lower-case snake_case key anywhere in the evidence (the names a model might copy)."""
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

    walk(json.loads(evidence_json))
    return names


def quoted_fields(explanation: str, names: set[str]) -> list[str]:
    """The field names the explanation uses as words."""

    def used(name: str) -> bool:
        word = rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])"
        return re.search(word, explanation) is not None

    return sorted(n for n in names if used(n))
