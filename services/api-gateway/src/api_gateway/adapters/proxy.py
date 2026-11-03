"""Reverse proxy: forward a public /v1 request to the service that owns it (spec x-upstream)."""

import logging

import httpx
from fastapi import Request, Response

from api_gateway.runtime.jsonlog import correlation_id_var
from api_gateway.runtime.problem import problem_response

logger = logging.getLogger("proxy")

# Connection-level headers that must not be forwarded as they are.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}
# Identity comes only from the verified JWT (Phase 2): a client must never set these itself.
_IDENTITY_HEADERS = {"x-trader-id", "x-role", "x-nickname"}


async def proxy_request(request: Request, upstream: str) -> Response:
    base_url = request.app.state.settings.service_upstreams.get(upstream)
    if base_url is None:
        return problem_response(
            503,
            "DEPENDENCY_UNAVAILABLE",
            "Upstream not configured",
            detail=f"No address is configured for {upstream}.",
            instance=request.url.path,
        )

    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP | _IDENTITY_HEADERS | {"x-correlation-id"}
    }
    correlation_id = correlation_id_var.get()
    if correlation_id:
        headers["X-Correlation-Id"] = correlation_id

    params: list[tuple[str, str | int | float | bool | None]] = list(
        request.query_params.multi_items()
    )
    client: httpx.AsyncClient = request.app.state.http_client
    try:
        upstream_response = await client.request(
            request.method,
            f"{base_url}{request.url.path}",
            params=params,
            headers=headers,
            content=await request.body(),
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "upstream unavailable", extra={"upstream": upstream, "error": type(exc).__name__}
        )
        return problem_response(
            503,
            "DEPENDENCY_UNAVAILABLE",
            "Upstream service unavailable",
            detail=f"{upstream} did not answer.",
            instance=request.url.path,
        )

    response_headers = {
        k: v
        for k, v in upstream_response.headers.items()
        if k.lower() not in _HOP_BY_HOP | {"content-encoding", "x-correlation-id"}
    }
    return Response(
        upstream_response.content,
        status_code=upstream_response.status_code,
        headers=response_headers,
    )
