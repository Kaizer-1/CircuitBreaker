"""Replay (Q-13): frames per command, paced by speed; pause, resume, speed; the end hash."""

import asyncio
import time
from typing import Any

import pytest

from market_data_service.application.replay import Pacer, PausedTooLongError, ReplaySession
from market_data_service.application.time_travel import TimeTravel
from market_data_service.domain.candles import bucket_of
from market_data_service.domain.market import Trade
from tests.fakes import Exchange, FakeEventStore, InMemoryMarketStore, ms

# ---------------------------------------------------------------------- the pacer's arithmetic


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_market_time_runs_at_the_speed_and_stops_while_paused() -> None:
    clock = FakeClock()
    pacer = Pacer(1_000_000, 10, pause_limit_seconds=300, clock=clock)
    clock.now += 2  # two wall seconds at 10x
    assert pacer.now_ms() == 1_020_000
    pacer.pause()
    clock.now += 60
    assert pacer.now_ms() == 1_020_000
    pacer.resume()
    clock.now += 1
    assert pacer.now_ms() == 1_030_000
    pacer.set_speed(60)
    clock.now += 0.5
    assert pacer.now_ms() == 1_060_000
    pacer.pause()
    pacer.set_speed(1)  # a speed change while paused does not move market time
    clock.now += 5
    assert pacer.now_ms() == 1_060_000


# ---------------------------------------------------------------------- sessions


def market(commands: int = 12, gap_seconds: float = 0.5) -> tuple[Exchange, list[Any]]:
    """A market with one command every `gap_seconds`; returns it and (time, depth) after each."""
    exchange = Exchange()
    after: list[Any] = []
    for n in range(commands):
        exchange.advance(gap_seconds)
        side = "SELL" if n % 2 == 0 else "BUY"
        exchange.place("abc"[n % 3], side, 5 + n % 4, 500 if n % 4 < 2 else 505)
        after.append((ms(exchange.as_of or ""), exchange.depth(), exchange.state_hash()))
    return exchange, after


def store_for(exchange: Exchange) -> InMemoryMarketStore:
    store = InMemoryMarketStore()
    store.trade_rows["CHAIWALA"] = [
        Trade(
            e.data["trade_id"],
            int(e.data["sequence"]),
            int(e.data["price_paise"]),
            int(e.data["quantity"]),
            e.data["aggressor_side"],
            ms(e.occurred_at),
        )
        for e in exchange.log
        if e.event_type == "TradeExecuted"
    ]
    return store


def session_for(exchange: Exchange, from_ms: int, to_ms: int, speed: int, **kw: Any) -> Any:
    events = FakeEventStore()
    events.events["CHAIWALA"] = exchange.log
    time_travel = TimeTravel(events, store_for(exchange))
    return ReplaySession(
        time_travel, ticker="CHAIWALA", from_ms=from_ms, to_ms=to_ms, speed=speed, **kw
    )


class Recorder:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.times: list[float] = []
        self._arrived = asyncio.Condition()

    async def __call__(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        self.times.append(time.monotonic())
        async with self._arrived:
            self._arrived.notify_all()

    async def wait_for(self, count: int) -> None:
        async with self._arrived:
            await self._arrived.wait_for(lambda: len(self.messages) >= count)

    def frames(self) -> list[dict[str, Any]]:
        return [m["data"] for m in self.messages if m["type"] == "frame"]


def levels(book: dict[str, Any]) -> tuple[list[tuple[int, int, int]], ...]:
    return tuple(
        [(lv["price_paise"], lv["quantity"], lv["order_count"]) for lv in book[side]]
        for side in ("bids", "asks")
    )


def test_a_replay_sends_the_book_at_from_then_one_frame_per_command_then_end() -> None:
    exchange, after = market()
    start, end = after[2][0], after[-1][0] + 300  # from just after the third command
    session = session_for(exchange, start, end, speed=60)
    sent = Recorder()
    began = time.monotonic()
    asyncio.run(session.run(sent))
    took = time.monotonic() - began

    frames = sent.frames()
    assert levels(frames[0]["order_book"]) == after[2][1]  # the market at `from`
    assert len(frames) == 1 + (len(after) - 3)  # then every later command
    for frame, (at, depth, _) in zip(frames[1:], after[3:], strict=True):
        assert ms(frame["as_of"]) == at
        assert levels(frame["order_book"]) == depth
    sequences = [f["as_of_sequence"] for f in frames]
    assert sequences == sorted(set(sequences))
    last = sent.messages[-1]
    assert last["type"] == "end"
    assert last["data"]["frames_sent"] == len(frames)
    assert last["data"]["state_hash"] == after[-1][2]  # the book at `to`
    assert took >= (end - start) / 1000 / 60 * 0.9  # paced by original time / speed


def test_the_frame_candle_is_the_forming_5s_candle_from_the_trades_so_far() -> None:
    exchange, after = market(commands=16, gap_seconds=0.9)
    trades = store_for(exchange).trade_rows["CHAIWALA"]
    session = session_for(exchange, after[0][0], after[-1][0], speed=60)
    sent = Recorder()
    asyncio.run(session.run(sent))
    for frame in sent.frames()[1:]:
        at = ms(frame["as_of"])
        bucket = bucket_of(at, 5_000)
        so_far = [t for t in trades if bucket <= t.executed_at_ms <= at]
        candle = frame["candle"]
        assert ms(candle["bucket_start"]) == bucket
        assert candle["trade_count"] == len(so_far)
        if so_far:
            assert candle["close_paise"] == so_far[-1].price_paise
            assert candle["volume"] == sum(t.quantity for t in so_far)


def test_a_sweep_is_one_frame_so_no_one_sees_half_a_match() -> None:
    exchange = Exchange()
    exchange.place("a", "SELL", 5, 500)
    exchange.place("b", "SELL", 5, 505)
    exchange.advance(1)
    start = ms(exchange.as_of or "")
    exchange.advance(1)
    events = exchange.place("c", "BUY", 10, 505)  # accepted + 2 trades: one command
    assert len(events) == 3
    session = session_for(exchange, start, ms(exchange.as_of or "") + 10, speed=60)
    sent = Recorder()
    asyncio.run(session.run(sent))
    frames = sent.frames()
    assert [f["as_of_sequence"] for f in frames] == [2, 5]
    assert frames[1]["last_trade"]["price_paise"] == 505
    assert frames[1]["order_book"]["asks"] == []


def test_pause_stops_the_frames_and_resume_carries_on() -> None:
    exchange, after = market(commands=6, gap_seconds=1.0)
    session = session_for(exchange, after[0][0], after[-1][0], speed=60)
    sent = Recorder()

    async def scenario() -> float:
        task = asyncio.create_task(session.run(sent))
        await sent.wait_for(2)
        session.pacer.pause()
        paused_at, count = time.monotonic(), len(sent.messages)
        await asyncio.sleep(0.3)
        assert len(sent.messages) == count  # nothing while paused
        session.pacer.resume()
        await task
        return paused_at

    paused_at = asyncio.run(scenario())
    assert sent.messages[-1]["type"] == "end"
    resumed = [t for t in sent.times if t > paused_at]
    assert resumed and resumed[0] - paused_at >= 0.3


def test_a_faster_speed_takes_effect_at_once() -> None:
    exchange, after = market(commands=4, gap_seconds=1.0)
    session = session_for(exchange, after[0][0], after[-1][0], speed=1)  # 3 s at 1x
    sent = Recorder()

    async def scenario() -> None:
        task = asyncio.create_task(session.run(sent))
        await sent.wait_for(1)
        session.pacer.set_speed(60)
        await task

    began = time.monotonic()
    asyncio.run(scenario())
    assert time.monotonic() - began < 1.0
    assert sent.messages[-1]["type"] == "end"


def test_a_replay_left_paused_too_long_ends() -> None:
    exchange, after = market(commands=4, gap_seconds=1.0)
    session = session_for(exchange, after[0][0], after[-1][0], speed=60, pause_limit_seconds=0.1)
    sent = Recorder()

    async def scenario() -> None:
        task = asyncio.create_task(session.run(sent))
        await sent.wait_for(1)
        session.pacer.pause()
        await task

    with pytest.raises(PausedTooLongError):
        asyncio.run(scenario())
    assert all(m["type"] == "frame" for m in sent.messages)
