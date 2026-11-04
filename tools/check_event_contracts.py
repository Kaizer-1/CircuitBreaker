# /// script
# requires-python = ">=3.12"
# dependencies = ["jsonschema[format-nongpl]==4.26.0"]
# ///
"""Keep contracts/events/*.schema.json equal to the event catalog, and check the catalog's examples.

The event catalog (docs/03-architecture/03-event-catalog.md) is the source of truth. Its JSON blocks are the envelope,
the shared $defs, one `<Event>.data` schema and one example per event. This tool turns them into one JSON Schema file
per event (envelope + that event's `data`), so producers' contract tests can validate what they publish.

    uv run tools/check_event_contracts.py           # check (make lint, CI): files match the catalog, examples valid
    uv run tools/check_event_contracts.py --write   # regenerate the files after editing the catalog
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "docs" / "03-architecture" / "03-event-catalog.md"
OUT_DIR = ROOT / "contracts" / "events"
BASE = "https://circuitbreaker.local/contracts/events"
DRAFT = "https://json-schema.org/draft/2020-12/schema"
GENERATED = (
    "Generated from docs/03-architecture/03-event-catalog.md by tools/check_event_contracts.py --write. "
    "Do not edit by hand: change the catalog and regenerate."
)

_TOPIC_ROW = re.compile(
    r"^\| `([a-z]+\.v\d+)` \| `([a-z-]+)` \| .*? \| `([a-z_]+)` \| \d+ \| .*? \| (.*) \|$"
)
_EVENT_HEADING = re.compile(r"^### `([A-Za-z]+)`")
_JSON_BLOCK = re.compile(r"^```json\n(.*?)\n```", re.S | re.M)


class Catalog:
    def __init__(self, text: str) -> None:
        self.topic_of: dict[str, tuple[str, str, str]] = {}  # event -> (topic, producer, key)
        for line in text.splitlines():
            if m := _TOPIC_ROW.match(line):
                topic, producer, key, events = m.groups()
                for event in re.findall(r"`([A-Za-z]+)`", events):
                    self.topic_of[event] = (topic, producer, key)

        self.envelope: dict[str, Any] = {}
        self.defs: dict[str, Any] = {}
        self.data: dict[str, dict[str, Any]] = {}
        self.examples: dict[str, list[dict[str, Any]]] = {}
        self.sections: list[str] = []
        for line in text.splitlines():
            if m := _EVENT_HEADING.match(line):
                self.sections.append(m.group(1))
        for raw in _JSON_BLOCK.findall(text):
            block = json.loads(raw)
            title = str(block.get("title", ""))
            if title == "EventEnvelope":
                self.envelope = block
            elif "$defs" in block and not title:
                self.defs = block["$defs"]
            elif title.endswith(".data"):
                self.data[title.removesuffix(".data")] = block
            elif "event_type" in block:
                self.examples.setdefault(block["event_type"], []).append(block)


def _rewrite_refs(node: Any) -> Any:
    """Catalog blocks say `#/$defs/x` (one page); the files keep the shared defs in defs.schema.json."""
    if isinstance(node, dict):
        return {
            k: (
                v.replace("#/$defs/", "defs.schema.json#/$defs/", 1)
                if k == "$ref" and isinstance(v, str) and v.startswith("#/$defs/")
                else _rewrite_refs(v)
            )
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_rewrite_refs(v) for v in node]
    return node


def build(catalog: Catalog) -> dict[str, dict[str, Any]]:
    files: dict[str, dict[str, Any]] = {
        "envelope.schema.json": {**catalog.envelope, "$comment": GENERATED},
        "defs.schema.json": {
            "$schema": DRAFT,
            "$id": f"{BASE}/defs.schema.json",
            "title": "Shared definitions for event schemas",
            "$comment": GENERATED,
            "$defs": catalog.defs,
        },
    }
    for event, data in sorted(catalog.data.items()):
        topic, producer, key = catalog.topic_of[event]
        files[f"{event}.schema.json"] = {
            "$schema": DRAFT,
            "$id": f"{BASE}/{event}.schema.json",
            "title": event,
            "$comment": GENERATED,
            "x-topic": topic,
            "x-key": key,
            "x-producer": producer,
            "allOf": [{"$ref": "envelope.schema.json"}],
            "properties": {
                "event_type": {"const": event},
                "event_version": {"const": 1},
                "producer": {"const": producer},
                "data": _rewrite_refs(data),
            },
        }
    return files


def render(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"


def consistency_problems(catalog: Catalog) -> list[str]:
    problems = []
    if not catalog.envelope or not catalog.defs:
        problems.append("catalog: envelope or shared $defs block not found")
    sections, in_topics, with_data = set(catalog.sections), set(catalog.topic_of), set(catalog.data)
    for event in sorted(sections | in_topics | with_data):
        if event not in in_topics:
            problems.append(f"{event}: not listed in the Topics table")
        if event not in sections:
            problems.append(f"{event}: no '### `{event}`' section")
        if event not in with_data:
            problems.append(f"{event}: no '{event}.data' schema block")
        if not catalog.examples.get(event):
            problems.append(f"{event}: no example")
    return problems


def example_problems(catalog: Catalog, files: dict[str, dict[str, Any]]) -> list[str]:
    registry: Registry[Any] = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in files.values()
    )
    problems = []
    for event, examples in sorted(catalog.examples.items()):
        schema = files.get(f"{event}.schema.json")
        if schema is None:
            problems.append(f"example of {event}: no schema")
            continue
        validator = Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())
        for example in examples:
            for error in validator.iter_errors(example):
                where = "/".join(str(p) for p in error.absolute_path) or "(root)"
                problems.append(f"example of {event}: {where}: {error.message}")
    return problems


def main(argv: list[str]) -> int:
    write = "--write" in argv
    catalog = Catalog(CATALOG.read_text())
    files = build(catalog)
    problems = consistency_problems(catalog) + example_problems(catalog, files)

    existing = {p.name for p in OUT_DIR.glob("*.schema.json")}
    if write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        for name, schema in files.items():
            (OUT_DIR / name).write_text(render(schema))
        for stale in sorted(existing - set(files)):
            (OUT_DIR / stale).unlink()
            print(f"removed stale {stale}")
    else:
        for name, schema in sorted(files.items()):
            path = OUT_DIR / name
            if not path.exists():
                problems.append(f"{name}: missing; run with --write")
            elif path.read_text() != render(schema):
                problems.append(f"{name}: differs from the catalog; run with --write")
        problems += [f"{name}: not in the catalog" for name in sorted(existing - set(files))]

    for problem in problems:
        print(f"CONTRACT {problem}")
    if problems:
        return 1
    count = sum(len(v) for v in catalog.examples.values())
    verb = "wrote" if write else "OK:"
    print(f"{verb} {len(files)} schema files match the event catalog; {count} examples valid")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
