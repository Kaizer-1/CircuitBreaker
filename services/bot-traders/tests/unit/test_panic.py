"""A-23/A-24: the panic sell (chaos scenarios section 3.3, ADR-0020)."""

import asyncio
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime
from random import Random

import pytest
from fastapi.testclient import TestClient

from bot_traders.application import panic as panic_module
from bot_traders.application.panic import PanicAlreadyRunningError, PanicSell
from bot_traders.config import Settings
from bot_traders.main import create_app
from bot_traders.ports.exchange import Session

NOW = datetime(2026, 11, 25, 9, 0, tzinfo=UTC)
PANIC = {"fault": "PANIC_SELL", "requests_per_second": 110, "duration_seconds": 300}


def seeded() -> Random:
    return Random(7)  # noqa: S311 (a seeded test double, not security)


class FakeExchange:
    def __init__(self, clock: list[float]) -> None:
        self.clock = clock
        self.sent: list[tuple[float, str, str, str]] = []  # (time, kind, account, ticker)
        self.gate: asyncio.Event | None = None  # set: answers wait for it
        self.tokens: set[str] = set()

        self.logins = 0

    async def login(self, account: str) -> Session:
        self.logins += 1
        return Session(account, f"id-{account}", f"token-{account}-{self.logins}")

    async def _answer(self) -> None:
        if self.gate is not None:
            await self.gate.wait()

    async def market_sell(self, session: Session, ticker: str) -> int:
        self.sent.append((self.clock[0], "SELL", session.account, ticker))
        self.tokens.add(session.token)
        await self._answer()
        return 202

    async def list_orders(self, session: Session) -> int:
        self.sent.append((self.clock[0], "GET", session.account, ""))
        await self._answer()
        return 200


def virtual_time() -> tuple[list[float], object]:
    now = [0.0]

    async def sleep(seconds: float) -> None:
        now[0] += seconds
        await asyncio.sleep(0)

    return now, sleep


async def test_requests_go_out_on_schedule_in_the_calibrated_mix() -> None:
    now, sleep = virtual_time()
    exchange = FakeExchange(now)
    results: Counter[tuple[str, str]] = Counter()
    panic = PanicSell(
        exchange,
        rng=seeded(),
        clock=lambda: now[0],
        sleep=sleep,  # type: ignore[arg-type]
        on_sent=lambda kind, result: results.update([(kind, result)]),
    )
    state = await panic.start(100, 2, NOW)
    assert (state.ends_at - state.started_at).total_seconds() == 2
    await panic.wait()

    assert len(exchange.sent) == 200
    assert results == Counter({("SELL", "202"): 180, ("GET", "200"): 20})  # 90% sells
    # Request n went out at n / rate (by the time its task ran, at most one slot later).
    assert all(0 <= t - n / 100 <= 0.01 + 1e-9 for n, (t, *_) in enumerate(exchange.sent))
    per_account = Counter(account for _, _, account, _ in exchange.sent)
    assert set(per_account) == {f"bot-{n:02d}" for n in range(1, 21)}
    assert set(per_account.values()) == {10}  # 5/s each at 100/s: far under the 50/s bot limit
    assert {ticker for _, kind, _, ticker in exchange.sent if kind == "SELL"} == {
        "CALVERA",
        "KORVIN",
        "ASTRYX",
        "HELIOR",
        "TAVIRA",
    }
    assert panic.state is None  # it ended by itself


async def test_it_never_waits_for_answers_but_bounds_what_is_unanswered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(panic_module, "MAX_IN_FLIGHT", 30)
    now, sleep = virtual_time()
    exchange = FakeExchange(now)
    exchange.gate = asyncio.Event()  # nothing is answered: order-service is drowning
    results: Counter[str] = Counter()
    panic = PanicSell(
        exchange,
        rng=seeded(),
        clock=lambda: now[0],
        sleep=sleep,  # type: ignore[arg-type]
        on_sent=lambda kind, result: results.update([result]),
    )
    await panic.start(50, 1, NOW)
    for _ in range(1_000):  # until the schedule has run out (virtual time)
        if panic.state is None:
            break
        await asyncio.sleep(0)
    assert len(exchange.sent) == 30  # sent on schedule without waiting, up to the bound
    assert results["dropped"] == 20
    exchange.gate.set()
    await panic.wait()
    assert results["202"] + results["200"] == 30


async def test_stopping_ends_it_at_once_and_a_second_start_is_refused() -> None:
    now, sleep = virtual_time()
    exchange = FakeExchange(now)
    panic = PanicSell(exchange, rng=seeded(), clock=lambda: now[0], sleep=sleep)  # type: ignore[arg-type]
    await panic.start(10, 300, NOW)
    with pytest.raises(PanicAlreadyRunningError):
        await panic.start(10, 300, NOW)
    for _ in range(5):
        await asyncio.sleep(0)
    assert await panic.stop()
    sent = len(exchange.sent)
    assert 0 < sent < 3000
    assert panic.state is None
    assert not await panic.stop()
    await asyncio.sleep(0)
    assert len(exchange.sent) == sent


async def test_every_burst_logs_in_afresh_so_no_token_is_from_an_earlier_one() -> None:
    """A token lasts 12 hours: a burst a day after the first must not reuse the first's
    tokens (staging, 2026-11-26: every request 401, no load reached order-service)."""
    now, sleep = virtual_time()
    exchange = FakeExchange(now)
    panic = PanicSell(exchange, rng=seeded(), clock=lambda: now[0], sleep=sleep)  # type: ignore[arg-type]
    for burst in (1, 2):
        exchange.tokens.clear()
        await panic.start(40, 2, NOW)
        await panic.wait()
        assert exchange.logins == 20 * burst
        first_token_of_burst = {f"token-bot-{n:02d}-{20 * (burst - 1) + n}" for n in range(1, 21)}
        assert exchange.tokens <= first_token_of_burst


@pytest.fixture
def hooks(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings.model_copy(update={"chaos_hooks_enabled": True}), readiness_checks=[])
    with TestClient(app) as client:
        app.state.panic = PanicSell(FakeExchange([0.0]), rng=seeded())
        yield client


def test_the_hooks_answer_404_unless_enabled(settings: Settings) -> None:
    with TestClient(create_app(settings, readiness_checks=[])) as client:
        assert client.post("/internal/v1/faults", json=PANIC).status_code == 404
        assert client.delete("/internal/v1/faults").status_code == 404


def test_start_then_refuse_a_second_then_stop(hooks: TestClient) -> None:
    started = hooks.post("/internal/v1/faults", json=PANIC)
    assert started.status_code == 201
    body = started.json()
    assert (body["fault"], body["active"], body["requests_per_second"]) == ("PANIC_SELL", True, 110)
    assert body["started_at"].endswith("Z") and body["ends_at"].endswith("Z")
    again = hooks.post("/internal/v1/faults", json=PANIC)
    assert (again.status_code, again.json()["code"]) == (409, "FAULT_ALREADY_ACTIVE")
    assert hooks.delete("/internal/v1/faults").status_code == 204
    gone = hooks.delete("/internal/v1/faults")
    assert (gone.status_code, gone.json()["code"]) == (404, "NO_ACTIVE_FAULT")


@pytest.mark.parametrize(
    "changes",
    [
        {"requests_per_second": 9},
        {"requests_per_second": 1001},
        {"duration_seconds": 901},
        {"fault": "MEMORY_LEAK"},
    ],
)
def test_a_bad_request_is_refused(hooks: TestClient, changes: dict[str, object]) -> None:
    response = hooks.post("/internal/v1/faults", json=PANIC | changes)
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")
