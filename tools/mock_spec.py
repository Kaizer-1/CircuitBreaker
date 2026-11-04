# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml==6.0.3"]
# ///
"""Build the spec the mock server serves: the public API with the owners' examples.

api/api-gateway.yaml references each response *schema* from the owning service's spec (one
definition of each shape), but the realistic *examples* live in those service specs. Prism mocks
from examples, so for every proxied operation (`x-upstream`) this copies the owner's response
examples into a build copy of the gateway spec. Nothing in api/ changes.

    uv run --locked tools/mock_spec.py      # writes web/mock/build/api/ (git-ignored)

`make mock` runs it, then Prism on the result (HTTP, :4010) and web/mock/ws-mock.mjs (the
WebSocket channels, :4011, which Prism cannot mock).
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "api"
OUT = ROOT / "web" / "mock" / "build" / "api"
METHODS = ("get", "post", "put", "delete", "patch")


def load(name: str) -> dict[str, Any]:
    spec: dict[str, Any] = yaml.safe_load((API / name).read_text())
    return spec


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    shutil.copytree(API, OUT)
    gateway = load("api-gateway.yaml")
    owners: dict[str, dict[str, Any]] = {}
    copied = 0
    for path, item in gateway["paths"].items():
        for method in METHODS:
            op = item.get(method)
            if not op or "x-upstream" not in op:
                continue
            owner = owners.setdefault(op["x-upstream"], load(f"{op['x-upstream']}.yaml"))
            source = owner["paths"].get(path, {}).get(method, {})
            for status, response in op.get("responses", {}).items():
                content = response.get("content", {}) if isinstance(response, dict) else {}
                theirs = source.get("responses", {}).get(status, {})
                for media, body in content.items():
                    found = (
                        theirs.get("content", {}).get(media, {}) if isinstance(theirs, dict) else {}
                    )
                    for key in ("example", "examples"):
                        if key in found and key not in body:
                            body[key] = found[key]
                            copied += 1
    (OUT / "api-gateway.yaml").write_text(
        yaml.safe_dump(gateway, sort_keys=False, allow_unicode=True)
    )
    print(f"mock spec: {copied} examples copied from the owners into {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
