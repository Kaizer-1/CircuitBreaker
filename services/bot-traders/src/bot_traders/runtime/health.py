"""Standard operational endpoints (docs/05-coding-standards.md section 7): /healthz and /readyz."""

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse


@dataclass(frozen=True)
class ReadinessCheck:
    """A dependency the service needs before it takes traffic. `probe` raises if it is unusable."""

    name: str
    probe: Callable[[], Awaitable[None]]


async def _run(check: ReadinessCheck, limit_seconds: float) -> dict[str, Any]:
    began = time.perf_counter()
    try:
        await asyncio.wait_for(check.probe(), limit_seconds)
        return {"ok": True, "latency_ms": round((time.perf_counter() - began) * 1000, 1)}
    except Exception as exc:
        return {
            "ok": False,
            "latency_ms": round((time.perf_counter() - began) * 1000, 1),
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        }


def health_router(
    service: str, checks: Sequence[ReadinessCheck], *, timeout: float = 3.0
) -> APIRouter:
    router = APIRouter()

    @router.get("/healthz", include_in_schema=False)
    async def liveness() -> dict[str, str]:
        # Liveness never checks dependencies: a database outage must not make Kubernetes restart us.
        return {"status": "ok"}

    @router.get("/readyz", include_in_schema=False)
    async def readiness() -> JSONResponse:
        results = await asyncio.gather(*(_run(c, timeout) for c in checks))
        by_name = {c.name: r for c, r in zip(checks, results, strict=True)}
        ready = all(r["ok"] for r in results)
        status = "ready" if ready else "not_ready"
        body = {"status": status, "service": service, "checks": by_name}
        return JSONResponse(body, status_code=200 if ready else 503)

    return router
