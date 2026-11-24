"""Smoke tests for the runtime plumbing that every service shares (same file in every service)."""

import json
import sys
import types
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api_gateway.config import Settings
from api_gateway.main import create_app
from api_gateway.runtime.events import (
    EventEnvelope,
    InvalidEventError,
    build_event,
    deterministic_event_id,
    format_timestamp,
    parse_timestamp,
)
from api_gateway.runtime.health import ReadinessCheck, health_router
from api_gateway.runtime.jsonlog import configure_logging, correlation_id_var
from api_gateway.runtime.kafka import (
    MAX_ATTEMPTS,
    Delivery,
    EventConsumer,
    EventPublisher,
    consumer_check,
)
from api_gateway.runtime.metrics import Metrics, metrics_router, watch_event_loop
from api_gateway.runtime.middleware import ObservabilityMiddleware
from api_gateway.runtime.problem import install_exception_handlers


async def _ok() -> None:
    return None


async def _down() -> None:
    raise ConnectionError("dependency is down")


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings, readiness_checks=[ReadinessCheck("fake-ok", _ok)])


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def plain_app(settings: Settings) -> FastAPI:
    """Only the shared runtime pieces, with the pass-through correlation ID that most services use.

    (The gateway issues its own correlation IDs; its tests cover that.)
    """
    metrics = Metrics(settings.service_name)
    app = FastAPI()
    app.add_middleware(
        ObservabilityMiddleware,
        service=settings.service_name,
        metrics=metrics,
        issue_correlation_id=False,
    )
    install_exception_handlers(app)
    app.include_router(health_router(settings.service_name, []))
    app.include_router(metrics_router(metrics))
    return app


def test_healthz_is_ok_even_when_a_dependency_is_down(settings: Settings) -> None:
    app = create_app(settings, readiness_checks=[ReadinessCheck("fake-down", _down)])
    with TestClient(app) as c:
        response = c.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_reports_ready_when_every_check_passes(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["fake-ok"]["ok"] is True


def test_readyz_returns_503_with_details_when_a_check_fails(settings: Settings) -> None:
    app = create_app(
        settings,
        readiness_checks=[ReadinessCheck("fake-ok", _ok), ReadinessCheck("fake-down", _down)],
    )
    with TestClient(app) as c:
        response = c.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["fake-ok"]["ok"] is True
    assert body["checks"]["fake-down"]["ok"] is False
    assert "dependency is down" in body["checks"]["fake-down"]["error"]


def test_metrics_exposes_the_standard_metrics(client: TestClient) -> None:
    client.get("/healthz")
    text = client.get("/metrics").text
    assert "http_requests_total{" in text
    assert 'route="/healthz"' in text
    assert "http_request_duration_seconds_bucket" in text
    assert "http_requests_in_flight" in text
    for name in ("events_published_total", "events_consumed_total", "consumer_lag_messages"):
        assert name in text


def test_correlation_id_is_generated_when_missing(plain_app: FastAPI) -> None:
    with TestClient(plain_app) as client:
        response = client.get("/healthz")
    assert len(response.headers["X-Correlation-Id"]) >= 32


def test_a_valid_incoming_correlation_id_is_passed_through(plain_app: FastAPI) -> None:
    with TestClient(plain_app) as client:
        response = client.get("/healthz", headers={"X-Correlation-Id": "from-gateway-123"})
    assert response.headers["X-Correlation-Id"] == "from-gateway-123"


def test_a_malformed_incoming_correlation_id_is_replaced(plain_app: FastAPI) -> None:
    with TestClient(plain_app) as client:
        response = client.get("/healthz", headers={"X-Correlation-Id": 'bad"id with spaces'})
    assert response.headers["X-Correlation-Id"] != 'bad"id with spaces'


def test_unknown_path_is_a_problem_details_404(plain_app: FastAPI) -> None:
    with TestClient(plain_app) as client:
        response = client.get("/v1/does-not-exist", headers={"X-Correlation-Id": "cid-1"})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == "NOT_FOUND"
    assert body["correlation_id"] == "cid-1"


def test_validation_errors_list_the_fields(plain_app: FastAPI) -> None:
    async def typed(limit: int) -> dict[str, int]:
        return {"limit": limit}

    plain_app.add_api_route("/test-only/typed", typed)
    with TestClient(plain_app) as c:
        response = c.get("/test-only/typed", params={"limit": "not-a-number"})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_FAILED"
    assert body["errors"][0]["field"] == "limit"


def test_a_body_that_is_not_json_is_a_400_bad_request(plain_app: FastAPI) -> None:
    async def takes_body(body: dict[str, int]) -> dict[str, int]:
        return body

    plain_app.add_api_route("/test-only/body", takes_body, methods=["POST"])
    with TestClient(plain_app) as c:
        response = c.post(
            "/test-only/body", content=b"{not json", headers={"content-type": "application/json"}
        )
    assert (response.status_code, response.json()["code"]) == (400, "BAD_REQUEST")


def test_an_unhandled_exception_becomes_a_problem_details_500(plain_app: FastAPI) -> None:
    async def boom() -> None:
        raise RuntimeError("secret internals must not leak")

    plain_app.add_api_route("/test-only/boom", boom)
    with TestClient(plain_app, raise_server_exceptions=False) as c:
        response = c.get("/test-only/boom", headers={"X-Correlation-Id": "cid-500"})
    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "INTERNAL_ERROR"
    assert body["correlation_id"] == "cid-500"
    assert "secret internals" not in response.text
    assert response.headers["X-Correlation-Id"] == "cid-500"


def test_problems_are_counted_by_error_code_even_from_a_worker_thread(
    plain_app: FastAPI,
) -> None:
    from api_gateway.runtime.problem import problem_response

    async def overloaded() -> Any:
        return problem_response(503, "OVERLOADED", "Overloaded")

    def from_a_thread() -> Any:  # FastAPI runs a plain def in a worker thread
        return problem_response(503, "DEPENDENCY_UNAVAILABLE", "Unavailable")

    async def boom() -> None:
        raise RuntimeError("bug")

    plain_app.add_api_route("/test-only/overloaded", overloaded)
    plain_app.add_api_route("/test-only/thread", from_a_thread)
    plain_app.add_api_route("/test-only/boom", boom)
    with TestClient(plain_app, raise_server_exceptions=False) as c:
        for path in ("/test-only/overloaded", "/test-only/thread", "/test-only/boom", "/healthz"):
            c.get(path)
        text = c.get("/metrics").text
    problems = sorted(line for line in text.splitlines() if line.startswith("http_problems_total{"))
    assert [line.split("code=")[1].split('"')[1] for line in problems] == [
        "DEPENDENCY_UNAVAILABLE",
        "INTERNAL_ERROR",
        "OVERLOADED",
    ]
    assert all(line.endswith(" 1.0") for line in problems)


def test_access_log_is_one_json_line_with_the_correlation_id(
    capsys: pytest.CaptureFixture[str], plain_app: FastAPI, settings: Settings
) -> None:
    configure_logging(settings.service_name, "INFO")  # binds the handler to the captured stdout
    with TestClient(plain_app) as c:
        c.get("/healthz", headers={"X-Correlation-Id": "log-test-1"})
    out = capsys.readouterr().out
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    entry = next(
        line for line in lines if line.get("message") == "request" and line["path"] == "/healthz"
    )
    assert entry["correlation_id"] == "log-test-1"
    assert entry["service"] == settings.service_name
    assert entry["status"] == 200
    assert entry["level"] == "INFO"
    assert "timestamp" in entry


# ------------------------------------------------- events and the consumer's delivery rules


def _event(event_type: str = "SomethingHappened") -> EventEnvelope:
    return build_event(
        event_type,
        {"answer": 42},
        producer="api-gateway",
        environment="dev",
        correlation_id="cid-ev",
        occurred_at=datetime(2026, 11, 20, 10, 42, 1, 250_000, tzinfo=UTC),
    )


def test_an_event_round_trips_through_json_and_ignores_unknown_fields() -> None:
    event = _event()
    assert event.occurred_at == "2026-11-20T10:42:01.250Z"
    raw = json.loads(event.to_json())
    raw["added_later"] = True  # tolerant reader
    assert EventEnvelope.from_json(json.dumps(raw).encode()) == event


def test_a_message_that_is_not_an_envelope_is_an_invalid_event() -> None:
    with pytest.raises(InvalidEventError):
        EventEnvelope.from_json(b"not json")
    with pytest.raises(InvalidEventError):
        EventEnvelope.from_json(b'{"event_type": "X"}')


def test_timestamps_and_deterministic_ids() -> None:
    moment = parse_timestamp("2026-11-20T10:42:01.250Z")
    assert format_timestamp(moment) == "2026-11-20T10:42:01.250Z"
    assert deterministic_event_id("https://x/1") == deterministic_event_id("https://x/1")
    assert deterministic_event_id("https://x/1") != deterministic_event_id("https://x/2")


class _Sink:
    def __init__(self) -> None:
        self.letters: list[tuple[str, bytes | None, dict[str, object]]] = []

    async def __call__(self, topic: str, key: bytes | None, value: bytes) -> None:
        self.letters.append((topic, key, json.loads(value)))


async def _no_sleep(_: float) -> None:
    return None


def _consumer(
    settings: Settings, handler: object, sink: _Sink, metrics: Metrics | None = None
) -> EventConsumer:
    return EventConsumer(
        bootstrap_servers="unused:9092",
        topics=["things.v1"],
        group_id=f"{settings.service_name}.things.v1",
        service=settings.service_name,
        metrics=metrics or Metrics(settings.service_name),
        handler=handler,  # type: ignore[arg-type]
        dead_letters=sink,
        sleep=_no_sleep,
    )


def _delivery(value: bytes) -> Delivery:
    return Delivery("things.v1", 0, 7, b"KEY", value)


class _FakeProducer:
    options: dict[str, Any] = {}

    def __init__(self, **options: Any) -> None:
        type(self).options = options

    async def start(self) -> None:
        return None


@pytest.mark.parametrize(
    ("deadline", "idempotent", "request_timeout_ms"),
    [(None, True, 10_000), (10.0, False, 3_333)],
)
async def test_a_publisher_with_a_deadline_is_not_idempotent_and_times_out_in_thirds(
    monkeypatch: pytest.MonkeyPatch,
    deadline: float | None,
    idempotent: bool,
    request_timeout_ms: int,
) -> None:
    # aiokafka never gives up on a message of an idempotent producer, so a delivery deadline
    # needs idempotence off; the integration test in api-gateway proves it on a real broker.
    monkeypatch.setitem(
        sys.modules, "aiokafka", types.SimpleNamespace(AIOKafkaProducer=_FakeProducer)
    )
    publisher = EventPublisher(
        "unused:9092", "svc", Metrics("svc"), delivery_timeout_seconds=deadline
    )
    await publisher.start()
    assert _FakeProducer.options["enable_idempotence"] is idempotent
    assert _FakeProducer.options["request_timeout_ms"] == request_timeout_ms
    assert _FakeProducer.options["acks"] == "all"


async def test_a_handled_message_is_not_dead_lettered(settings: Settings) -> None:
    seen: list[str] = []

    async def handler(event: EventEnvelope, delivery: Delivery) -> None:
        seen.append(event.event_id)

    sink = _Sink()
    event = _event()
    assert await _consumer(settings, handler, sink).handle(_delivery(event.to_json())) is True
    assert seen == [event.event_id]
    assert sink.letters == []


async def test_a_handled_event_is_logged_under_its_own_correlation_id(
    capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    # One Loki query by correlation ID follows an order through every service that reacts to it.
    import logging

    async def handler(event: EventEnvelope, delivery: Delivery) -> None:
        logging.getLogger("handler").info("inside the handler")

    configure_logging(settings.service_name, "INFO")
    event = _event()
    assert await _consumer(settings, handler, _Sink()).handle(_delivery(event.to_json()))
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line[:1] == "{"]
    inside = next(line for line in lines if line["message"] == "inside the handler")
    handled = next(line for line in lines if line["message"] == "event handled")
    assert inside["correlation_id"] == handled["correlation_id"] == "cid-ev"
    assert handled["event_type"] == "SomethingHappened"
    assert handled["event_id"] == event.event_id
    assert correlation_id_var.get() is None  # restored afterwards: the next event gets its own


async def test_a_message_failing_five_times_goes_to_the_dlq_with_the_error(
    settings: Settings,
) -> None:
    calls = 0

    async def handler(event: EventEnvelope, delivery: Delivery) -> None:
        nonlocal calls
        calls += 1
        raise ValueError("bad data in the message")

    sink, metrics = _Sink(), Metrics(settings.service_name)
    consumer = _consumer(settings, handler, sink, metrics)
    dead = metrics.events_dead_lettered_total.labels(settings.service_name, "things.v1")
    assert dead._value.get() == 0  # exported from the start, so the first one is an increase
    assert await consumer.handle(_delivery(_event().to_json())) is False
    assert calls == MAX_ATTEMPTS
    assert dead._value.get() == 1  # what the DeadLetterMessages alert watches
    [(topic, key, body)] = sink.letters
    assert topic == "things.v1.dlq"
    assert key == b"KEY"
    assert body["attempts"] == MAX_ATTEMPTS
    assert "bad data in the message" in str(body["error"])
    assert body["offset"] == 7


async def test_an_infrastructure_outage_is_retried_and_never_dead_lettered(
    settings: Settings,
) -> None:
    calls = 0

    async def handler(event: EventEnvelope, delivery: Delivery) -> None:
        nonlocal calls
        calls += 1
        if calls <= MAX_ATTEMPTS * 2:
            raise ConnectionError("database is down")

    sink = _Sink()
    assert await _consumer(settings, handler, sink).handle(_delivery(_event().to_json())) is True
    assert calls == MAX_ATTEMPTS * 2 + 1
    assert sink.letters == []


async def test_a_message_that_is_not_an_event_goes_straight_to_the_dlq(settings: Settings) -> None:
    async def handler(event: EventEnvelope, delivery: Delivery) -> None:
        raise AssertionError("must not be called")

    sink = _Sink()
    assert await _consumer(settings, handler, sink).handle(_delivery(b"{broken")) is False
    assert sink.letters[0][2]["attempts"] == 1


async def test_the_consumer_readiness_check_waits_until_it_has_caught_up(
    settings: Settings,
) -> None:
    async def handler(event: EventEnvelope, delivery: Delivery) -> None:
        return None

    consumer = _consumer(settings, handler, _Sink())
    for current in (None, consumer):
        with pytest.raises(RuntimeError):
            await consumer_check(lambda current=current: current).probe()  # type: ignore[misc]
    consumer.caught_up_once = True
    await consumer_check(lambda: consumer).probe()


async def test_the_event_loop_watcher_records_how_late_the_loop_woke() -> None:
    import asyncio
    import time

    metrics = Metrics("svc")
    watcher = asyncio.create_task(watch_event_loop(metrics, interval=0.01))
    await asyncio.sleep(0.02)
    time.sleep(0.15)  # noqa: ASYNC251 (blocks the loop on purpose: the watcher wakes late)
    await asyncio.sleep(0.03)
    watcher.cancel()
    late = metrics.registry.get_sample_value(
        "event_loop_lag_seconds_bucket", {"service": "svc", "le": "0.1"}
    )
    total = metrics.registry.get_sample_value("event_loop_lag_seconds_count", {"service": "svc"})
    assert total is not None and late is not None and total - late >= 1  # one wake over 0.1 s
