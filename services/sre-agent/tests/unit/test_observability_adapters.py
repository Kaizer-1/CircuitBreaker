"""Prometheus and Loki adapters against recorded-shape answers (httpx MockTransport)."""

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from sre_agent.adapters.loki import LokiLogs
from sre_agent.adapters.prometheus import PrometheusMetrics, _parse
from sre_agent.ports.observability import SourceUnavailableError

RULES = {
    "status": "success",
    "data": {
        "groups": [
            {
                "rules": [
                    {
                        "name": "OrderServiceHighErrorRate",
                        "duration": 20,
                        "alerts": [
                            {
                                "labels": {"namespace": "staging", "service": "order-service",
                                           "severity": "critical", "remediable": "true"},
                                "annotations": {"summary": "order-service errors at 38%"},
                                "state": "firing",
                                "activeAt": "2026-11-26T11:05:11.123456789Z",
                            },
                            {"labels": {"namespace": "prod", "service": "order-service"},
                             "state": "firing", "activeAt": "2026-11-26T11:05:11Z"},
                            {"labels": {"namespace": "staging", "service": "order-service"},
                             "state": "pending", "activeAt": "2026-11-26T11:05:11Z"},
                        ],
                    },
                    {
                        "name": "OrderServiceCircuitOpen",
                        "duration": 0,
                        "alerts": [{"labels": {"namespace": "staging", "service": "order-service",
                                               "remediable": "false"},
                                    "state": "firing", "activeAt": "2026-11-26T11:06:00Z"}],
                    },
                ]
            }
        ]
    },
}  # fmt: skip


def client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_only_this_namespaces_firing_alerts_with_their_firing_time() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/rules" and request.url.params["type"] == "alert"
        return httpx.Response(200, json=RULES)

    async with client(handler) as c:
        alerts = await PrometheusMetrics(c, "http://p:9090", "staging").firing_alerts()
    assert [(a.name, a.remediable) for a in alerts] == [
        ("OrderServiceHighErrorRate", True),
        ("OrderServiceCircuitOpen", False),
    ]
    assert alerts[0].fired_at == datetime(2026, 11, 26, 11, 5, 31, 123456, tzinfo=UTC)
    assert alerts[0].summary == "order-service errors at 38%"


async def test_values_sum_series_by_label_and_nan_means_no_answer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        if "nan" in query:
            result = [{"metric": {}, "value": [1, "NaN"]}]
        else:
            result = [{"metric": {"code": "OVERLOADED"}, "value": [1, "31"]},
                      {"metric": {"code": "INTERNAL_ERROR"}, "value": [1, "0.2"]}]  # fmt: skip
        return httpx.Response(200, json={"status": "success", "data": {"result": result}})

    async with client(handler) as c:
        m = PrometheusMetrics(c, "http://p:9090", "staging")
        assert await m.value("sum(x)") == pytest.approx(31.2)
        assert await m.value("nan") is None
        assert await m.by_label("sum by (code) (x)", "code") == {
            "OVERLOADED": 31.0, "INTERNAL_ERROR": 0.2}  # fmt: skip


async def test_a_range_query_is_aligned_to_its_steps_and_gaps_stay_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        start = float(request.url.params["start"])
        values = [[start, "1"], [start + 60, "3"]]  # the point at +30 s is missing
        return httpx.Response(
            200, json={"status": "success", "data": {"result": [{"values": values}]}}
        )

    async with client(handler) as c:
        points = await PrometheusMetrics(c, "http://p", "staging").series("q", 1, 30)
    assert points == [1.0, None, 3.0]


async def test_an_error_or_a_failed_query_is_a_missing_source() -> None:
    async with client(lambda r: httpx.Response(503)) as c:
        with pytest.raises(SourceUnavailableError):
            await PrometheusMetrics(c, "http://p", "staging").value("q")
    bad = {"status": "error", "error": "parse error"}
    async with client(lambda r: httpx.Response(200, json=bad)) as c:
        with pytest.raises(SourceUnavailableError, match="parse error"):
            await PrometheusMetrics(c, "http://p", "staging").value("q")


def test_nanosecond_timestamps_are_read() -> None:
    assert _parse("2026-11-26T11:05:11Z") == datetime(2026, 11, 26, 11, 5, 11, tzinfo=UTC)


async def test_loki_lines_newest_first_and_top_messages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        assert 'namespace="staging",app="order-service",level=~"ERROR|CRITICAL"' in query
        if request.url.path.endswith("query_range"):
            streams = [
                {"values": [["100", "old"], ["300", "newest"]]},
                {"values": [["200", "mid"]]},
            ]
            return httpx.Response(200, json={"data": {"result": streams}})
        rows = [{"metric": {"message": "boom"}, "value": [1, "412"]},
                {"metric": {"message": "rare"}, "value": [1, "3"]}]  # fmt: skip
        return httpx.Response(200, content=json.dumps({"data": {"result": rows}}))

    async with client(handler) as c:
        logs = LokiLogs(c, "http://loki:3100", "staging")
        assert await logs.error_lines("order-service", 15, 2) == ["newest", "mid"]
        assert await logs.top_errors("order-service", 15, 5) == [("boom", 412), ("rare", 3)]
