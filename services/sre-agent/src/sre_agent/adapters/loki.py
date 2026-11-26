"""Loki over its HTTP API: a service's newest error lines and its most frequent error messages.

The streams carry the labels namespace, app, container and level (Alloy, ADR-0018); our services
log one JSON object per line, so the message is `| json | line_format "{{.message}}"`.
"""

import time
from typing import Any

import httpx

from sre_agent.ports.observability import SourceUnavailableError


class LokiLogs:
    def __init__(self, client: httpx.AsyncClient, base_url: str, namespace: str) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._namespace = namespace

    def _selector(self, service: str) -> str:
        return f'{{namespace="{self._namespace}",app="{service}",level=~"ERROR|CRITICAL"}}'

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        try:
            response = await self._client.get(f"{self._base}{path}", params=params, timeout=8.0)
            response.raise_for_status()
            return response.json()["data"]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise SourceUnavailableError(f"Loki: {type(exc).__name__}") from exc

    async def error_lines(self, service: str, minutes: int, limit: int) -> list[str]:
        now = time.time_ns()
        data = await self._get(
            "/loki/api/v1/query_range",
            {
                "query": self._selector(service),
                "start": now - minutes * 60 * 1_000_000_000,
                "end": now,
                "limit": limit,
                "direction": "backward",
            },
        )
        entries: list[tuple[int, str]] = []
        for stream in data.get("result", []):
            entries += [(int(ts), line) for ts, line in stream.get("values", [])]
        return [line for _, line in sorted(entries, reverse=True)[:limit]]

    async def top_errors(self, service: str, minutes: int, k: int) -> list[tuple[str, int]]:
        query = (
            f"topk({k}, sum by (message) (count_over_time({self._selector(service)}"
            f' | json | __error__="" [{minutes}m])))'
        )
        data = await self._get("/loki/api/v1/query", {"query": query})
        rows = []
        for r in data.get("result", []):
            message = r.get("metric", {}).get("message", "")
            try:
                count = int(float(r["value"][1]))
            except (KeyError, ValueError, IndexError):
                continue
            rows.append((message, count))
        return sorted(rows, key=lambda row: -row[1])[:k]
