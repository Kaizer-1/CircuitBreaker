"""Prometheus over its HTTP API: firing alerts (from the rules API, which also gives each rule's
`for`), instant queries and range queries. Read-only.

Alerts are evaluated `by (namespace)`, so each environment's agent keeps only its own namespace's.
An alert's FIRING time is when it became pending plus the rule's `for` (the API gives `activeAt`,
the pending start): the "detect" time of the approved timing definitions is measured from it.
"""

from datetime import datetime, timedelta
from typing import Any

import httpx

from sre_agent.ports.observability import Alert, SourceUnavailableError


class PrometheusMetrics:
    def __init__(self, client: httpx.AsyncClient, base_url: str, namespace: str) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._namespace = namespace

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        try:
            response = await self._client.get(f"{self._base}{path}", params=params, timeout=8.0)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SourceUnavailableError(f"Prometheus: {type(exc).__name__}") from exc
        if body.get("status") != "success":
            raise SourceUnavailableError(f"Prometheus: {body.get('error', 'query failed')}")
        return body["data"]

    async def firing_alerts(self) -> list[Alert]:
        data = await self._get("/api/v1/rules", {"type": "alert"})
        alerts = []
        for group in data.get("groups", []):
            for rule in group.get("rules", []):
                hold = float(rule.get("duration") or 0)
                for a in rule.get("alerts", []):
                    labels: dict[str, str] = a.get("labels", {})
                    if a.get("state") != "firing" or labels.get("namespace") != self._namespace:
                        continue
                    if "service" not in labels:
                        continue
                    active = a.get("activeAt")
                    fired = _parse(active) + timedelta(seconds=hold) if active else None
                    alerts.append(
                        Alert(
                            name=rule.get("name") or labels.get("alertname", "?"),
                            service=labels["service"],
                            severity=labels.get("severity", "warning"),
                            remediable=labels.get("remediable") == "true",
                            summary=a.get("annotations", {}).get("summary", rule.get("name", "")),
                            fired_at=fired,
                            labels=labels,
                        )
                    )
        return alerts

    async def _vector(self, query: str) -> list[dict[str, Any]]:
        data = await self._get("/api/v1/query", {"query": query})
        result: list[dict[str, Any]] = data.get("result", []) if isinstance(data, dict) else []
        return result

    async def value(self, query: str) -> float | None:
        rows = await self._vector(query)
        numbers = [_number(r["value"][1]) for r in rows]
        present = [n for n in numbers if n is not None]
        return sum(present) if present else None

    async def by_label(self, query: str, label: str) -> dict[str, float]:
        out: dict[str, float] = {}
        for r in await self._vector(query):
            n = _number(r["value"][1])
            if n is not None:
                key = r.get("metric", {}).get(label, "")
                out[key] = out.get(key, 0.0) + n
        return out

    async def _range(
        self, query: str, minutes: int, step_seconds: int
    ) -> tuple[list[dict[str, Any]], float, int]:
        end = datetime.now().timestamp()
        start = end - minutes * 60
        data = await self._get(
            "/api/v1/query_range",
            {"query": query, "start": start, "end": end, "step": step_seconds},
        )
        return data.get("result", []), start, int(minutes * 60 // step_seconds) + 1

    @staticmethod
    def _aligned(row: dict[str, Any], start: float, step: int, points: int) -> list[float | None]:
        out: list[float | None] = [None] * points
        for ts, raw in row.get("values", []):
            i = round((float(ts) - start) / step)
            if 0 <= i < points:
                out[i] = _number(raw)
        return out

    async def series(self, query: str, minutes: int, step_seconds: int) -> list[float | None]:
        rows, start, points = await self._range(query, minutes, step_seconds)
        if not rows:
            return [None] * points
        return self._aligned(rows[0], start, step_seconds, points)  # the queries aggregate

    async def series_by_label(
        self, query: str, label: str, minutes: int, step_seconds: int
    ) -> dict[str, list[float | None]]:
        rows, start, points = await self._range(query, minutes, step_seconds)
        return {
            str(r.get("metric", {}).get(label, "")): self._aligned(r, start, step_seconds, points)
            for r in rows
        }


def _number(raw: Any) -> float | None:
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return None
    return None if n != n or n in (float("inf"), float("-inf")) else n  # NaN, Inf: no answer


def _parse(value: str) -> datetime:
    # Prometheus writes nanoseconds ("2026-11-26T11:05:31.123456789Z"); Python reads microseconds.
    head, _, tail = value.rstrip("Z").partition(".")
    fraction = (tail + "000000")[:6] if tail else "000000"
    return datetime.fromisoformat(f"{head}.{fraction}+00:00")
