#!/usr/bin/env python3
"""Generate FastAPI stub routes (HTTP 501) for every operation in an OpenAPI spec.

    python3 tools/gen_stubs.py order-service            # writes services/order-service/src/order_service/api/stubs.py
    python3 tools/gen_stubs.py order-service <file>     # write somewhere else

The output is committed. It is generated ONCE (contract first, SEED(S) step 5); from then on the service owns
the file and replaces each stub with a real route as the operation is built. tests/contract/ fails if the
file and the spec ever disagree. For api-gateway, operations that name an `x-upstream` become proxy routes.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
METHODS = ("get", "post", "put", "delete", "patch")


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def render(service: str) -> str:
    package = service.replace("-", "_")
    spec = yaml.safe_load((ROOT / "api" / f"{service}.yaml").read_text())
    gateway = service == "api-gateway"

    routes: list[str] = []
    proxied = stubbed = 0
    for path, item in spec["paths"].items():
        for method in METHODS:
            op = item.get(method)
            if not op:
                continue
            seeds_id = op["x-seeds-id"]
            upstream = op.get("x-upstream")
            func = snake(op["operationId"])
            decorator = (
                f"@router.{method}(\n"
                f"    {path!r},\n"
                f"    operation_id={op['operationId']!r},\n"
                f"    summary={op['summary']!r},\n"
                f"    openapi_extra={{'x-seeds-id': {seeds_id!r}}},\n"
                f")"
            )
            if gateway and upstream:
                proxied += 1
                body = f"async def {func}(request: Request) -> Response:\n    return await proxy_request(request, {upstream!r})"
            else:
                stubbed += 1
                body = f"async def {func}(request: Request) -> JSONResponse:\n    return not_implemented(request, {seeds_id!r})"
            routes.append(f"{decorator}\n{body}\n")

    imports = ["from fastapi import APIRouter, Request"]
    responses = (["JSONResponse"] if stubbed else []) + (["Response"] if proxied else [])
    imports.append(f"from fastapi.responses import {', '.join(responses)}")
    if proxied:
        imports.append(f"from {package}.adapters.proxy import proxy_request")
    if stubbed:
        imports.append(f"from {package}.runtime.problem import not_implemented")

    header = (
        f'"""Stub routes for every operation in api/{service}.yaml.\n\n'
        "Generated once by tools/gen_stubs.py (contract first). From now on edit this file by hand:\n"
        "replace a stub with the real route when the operation is built. tests/contract/ fails if this\n"
        'file and the spec disagree.\n"""\n'
    )
    return header + "\n".join(imports) + "\n\nrouter = APIRouter()\n\n\n" + "\n\n".join(routes)


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print(__doc__)
        return 2
    service = sys.argv[1]
    package = service.replace("-", "_")
    default = ROOT / "services" / service / "src" / package / "api" / "stubs.py"
    out = Path(sys.argv[2]) if len(sys.argv) == 3 else default
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(service))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
