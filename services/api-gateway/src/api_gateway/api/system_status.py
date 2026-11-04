"""GET /v1/system/status: ask every service's /readyz and combine the answers.

An operational endpoint, like /healthz: it has no SEED(S) ID and is not in api/api-gateway.yaml
(see docs/05-coding-standards.md section 7). Admin only since Phase 2.
"""

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request

from api_gateway.api.auth import ADMIN

SERVICE_ORDER = (
    "order-service",
    "matching-engine",
    "market-data-service",
    "portfolio-service",
    "bot-traders",
    "sre-agent",
    "chaos-service",
)

router = APIRouter()


async def _probe(
    client: httpx.AsyncClient, name: str, base_url: str, limit_seconds: float
) -> dict[str, Any]:
    began = time.perf_counter()
    try:
        response = await client.get(f"{base_url}/readyz", timeout=limit_seconds)
        body = response.json()
        if response.status_code == 200:
            status = "ready"
        elif response.status_code == 503:
            status = "not_ready"
        else:
            return {"name": name, "status": "unreachable", "error": f"HTTP {response.status_code}"}
        return {
            "name": name,
            "status": status,
            "latency_ms": round((time.perf_counter() - began) * 1000, 1),
            "checks": body.get("checks", {}),
        }
    except (httpx.HTTPError, ValueError) as exc:
        return {"name": name, "status": "unreachable", "error": type(exc).__name__}


def _dependencies(services: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group the per-service checks (redpanda, cassandra, postgres) into one row per dependency."""
    used_by: dict[str, list[dict[str, Any]]] = {}
    for service in services:
        for dependency, result in service.get("checks", {}).items():
            used_by.setdefault(dependency, []).append(
                {"service": service["name"], "ok": bool(result.get("ok"))}
            )
    rows = []
    for dependency, users in sorted(used_by.items()):
        oks = [u["ok"] for u in users]
        status = "ok" if all(oks) else "down" if not any(oks) else "degraded"
        rows.append({"name": dependency, "status": status, "used_by": users})
    return rows


@router.get("/v1/system/status", include_in_schema=False, dependencies=[Depends(ADMIN)])
async def system_status(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    client: httpx.AsyncClient = request.app.state.http_client
    upstreams: dict[str, str] = settings.service_upstreams
    probes = [
        _probe(client, name, upstreams[name], settings.status_timeout_seconds)
        for name in SERVICE_ORDER
        if name in upstreams
    ]
    services = [{"name": settings.service_name, "status": "ready", "checks": {}}]
    services += await asyncio.gather(*probes)
    healthy = all(s["status"] == "ready" for s in services)
    return {
        "status": "healthy" if healthy else "unhealthy",
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "services": services,
        "dependencies": _dependencies(services),
    }
