"""The projection: strict sequence order, gaps filled from the event store, catch-up at start,
candles closed by the clock only while caught up, everything saved before the next event."""

from datetime import timedelta

import pytest

from market_data_service.application.projection import MarketProjection
from market_data_service.events.candles import format_bucket
from market_data_service.runtime.events import build_event, deterministic_event_id
from tests.fakes import Clock, Exchange, FakeEventStore, InMemoryMarketStore, RecordingPublisher

A, B = "3d2a1b0c-9e8f-4a7b-8c6d-5e4f3a2b1c0d", "b0b0b0b0-0003-4000-8000-000000000003"


class World:
    def __init__(self) -> None:
        self.clock = Clock()
        self.exchange = Exchange(clock=self.clock)
        self.events = FakeEventStore()
        self.events.events["CALVERA"] = self.exchange.log  # the engine stores everything
        self.store = InMemoryMarketStore()
        self.publisher = RecordingPublisher()
        self.caught_up = True
        self.projection = self.new_projection()

    def new_projection(self) -> MarketProjection:
        async def no_sleep(_: float) -> None:
            self.store.down = False  # the store "recovers" while we wait

        return MarketProjection(
            self.store,
            self.events,
            self.publisher,
            environment="dev",
            clock=self.clock,
            sleep=no_sleep,
            feed_caught_up=lambda: self.caught_up,
        )

    def trade(self, price: int = 24600, quantity: int = 10) -> list:  # type: ignore[type-arg]
        self.exchange.place(B, "SELL", quantity, price)
        return self.exchange.place(A, "BUY", quantity, price)

    async def deliver_all(self, start: int = 0) -> None:
        for event in self.exchange.log[start:]:
            await self.projection.handle(event)


@pytest.fixture
async def w() -> World:
    world = World()
    await world.projection.start()
    return world


async def test_a_fresh_start_with_no_history_is_ready_and_flat_at_the_reference_price(
    w: World,
) -> None:
    market = w.projection.markets["CALVERA"]
    assert w.projection.ready and market.sequence == 0
    assert market.last_price_paise == 24550
    w.clock.now += timedelta(seconds=11)
    await w.projection.tick()
    closed = w.publisher.candles_closed()
    assert [c["interval"] for c in closed] == ["5s", "5s"]
    assert {c["close_paise"] for c in closed} == {24550} and closed[0]["volume"] == 0


async def test_events_apply_in_order_and_a_repeat_changes_nothing(w: World) -> None:
    w.trade(24600, 10)
    await w.deliver_all()
    saves = w.store.saves
    await w.deliver_all()  # every event again
    market = w.projection.markets["CALVERA"]
    assert market.sequence == 3 and market.last_price_paise == 24600  # accepted, accepted, traded
    assert [t.quantity for t in market.recent] == [10]
    assert w.store.saves == saves


async def test_a_gap_is_filled_from_the_event_store_before_the_newer_event(w: World) -> None:
    w.trade(24600, 10)
    w.trade(24700, 5)
    reads = w.events.reads
    await w.projection.handle(w.exchange.log[-1])  # sequence 6 arrives first: 1-5 are missing
    market = w.projection.markets["CALVERA"]
    assert market.sequence == 6
    assert [t.price_paise for t in market.recent] == [24700, 24600]
    assert w.events.reads == reads + 1  # one read of the event store fills the gap


async def test_every_event_is_saved_with_its_sequence_before_the_next(w: World) -> None:
    w.trade(24600, 10)
    await w.deliver_all()
    row = w.store.rows["CALVERA"]
    assert row.sequence == 3 and row.last_price_paise == 24600
    assert [t.sequence for t in w.store.trade_rows["CALVERA"]] == [3]


async def test_a_store_outage_is_waited_out_and_nothing_is_lost(w: World) -> None:
    w.trade(24600, 10)
    w.store.down = True
    await w.deliver_all()
    assert w.store.rows["CALVERA"].sequence == 3


async def test_the_clock_closes_candles_only_while_the_feed_is_caught_up(w: World) -> None:
    w.trade(24600, 10)
    await w.deliver_all()
    w.clock.now += timedelta(seconds=6)
    w.caught_up = False  # a trade for this bucket may still be on its way
    await w.projection.tick()
    assert w.publisher.candles_closed() == []
    w.caught_up = True
    await w.projection.tick()
    [closed] = w.publisher.candles_closed()
    assert (closed["interval"], closed["close_paise"], closed["volume"]) == ("5s", 24600, 10)


async def test_the_clock_waits_half_a_second_after_the_bucket_ended(w: World) -> None:
    w.clock.now = w.clock.now.replace(second=5, microsecond=200_000)  # 0.2 s after 10:42:05
    await w.projection.tick()
    assert w.publisher.candles_closed() == []
    w.clock.now += timedelta(milliseconds=400)
    await w.projection.tick()
    assert len(w.publisher.candles_closed()) == 1


async def test_candle_closed_has_the_deterministic_id_and_the_catalog_fields(w: World) -> None:
    w.trade(24600, 10)
    await w.deliver_all()
    w.clock.now += timedelta(seconds=6)
    await w.projection.tick()
    [(topic, key, event)] = [s for s in w.publisher.sent if s[1] == "CALVERA"]
    start = event.data["bucket_start"]
    assert (topic, key, start) == ("candles.v1", "CALVERA", "2026-11-20T10:42:00Z")
    assert event.data["bucket_end"] == "2026-11-20T10:42:05Z"
    assert event.event_id == deterministic_event_id(f"CALVERA|5s|{start}")
    assert event.correlation_id == f"candle-CALVERA-5s-{start}"


async def test_redpanda_down_drops_candle_closed_but_keeps_the_stored_candle(w: World) -> None:
    w.trade(24600, 10)
    await w.deliver_all()
    w.publisher.down = True
    w.clock.now += timedelta(seconds=6)
    await w.projection.tick()
    bucket = int(w.clock.now.replace(second=0).timestamp() * 1000)
    assert w.store.candle_rows[("CALVERA", "5s", bucket)].volume == 10
    assert format_bucket(bucket) == "2026-11-20T10:42:00Z"


async def test_a_malformed_event_is_dead_lettered_and_skipped(w: World) -> None:
    bad = build_event(
        "TradeExecuted",
        {"sequence": 1, "ticker": "CALVERA"},  # no price, no quantity
        producer="matching-engine",
        environment="dev",
        correlation_id="x",
    )
    await w.projection.handle(bad)
    assert w.projection.markets["CALVERA"].sequence == 1
    assert [topic for topic, _ in w.publisher.raw] == ["matching.v1.dlq"]


async def test_an_event_without_a_known_ticker_goes_to_the_dead_letter_topic(w: World) -> None:
    odd = build_event(
        "TradeExecuted", {"ticker": "NOPE"}, producer="x", environment="dev", correlation_id="x"
    )
    await w.projection.handle(odd)
    assert [topic for topic, _ in w.publisher.raw] == ["matching.v1.dlq"]


async def test_a_restart_rebuilds_exactly_the_same_market(w: World) -> None:
    for price in (24600, 24650, 24500):
        w.trade(price, 7)
        w.clock.now += timedelta(seconds=3)
    w.exchange.place(B, "SELL", 5, 24800)  # resting ask
    w.exchange.place(A, "BUY", 3, 24400)  # resting bid
    await w.deliver_all()
    w.clock.now += timedelta(seconds=10)
    await w.projection.tick()
    before = w.projection.markets["CALVERA"]

    w.events.snapshots["CALVERA"] = w.exchange.snapshot()
    again = w.new_projection()
    await again.start()
    await again.tick()  # the same clock: closes what the first one had closed
    after = again.markets["CALVERA"]
    assert after.sequence == before.sequence
    assert after.depth_levels(10) == before.depth_levels(10)
    assert list(after.recent) == list(before.recent)
    assert after.last_price_paise == before.last_price_paise
    assert after.series is not None and before.series is not None
    for interval in ("5s", "1m", "5m"):
        a, b = after.series[interval], before.series[interval]
        assert a.candles(0, b.forming_start_ms) == b.candles(0, b.forming_start_ms)


async def test_a_start_from_scratch_rebuilds_the_market_from_the_event_store(w: World) -> None:
    # Dropping every market-data table and starting again: the engine's history is enough.
    for price in (24600, 24650):
        w.trade(price, 7)
        w.clock.now += timedelta(seconds=6)
    await w.deliver_all()
    live = w.projection.markets["CALVERA"]
    w.store = InMemoryMarketStore()
    rebuilt = w.new_projection()
    await rebuilt.start()
    again = rebuilt.markets["CALVERA"]
    assert again.sequence == live.sequence and list(again.recent) == list(live.recent)
    assert again.series and live.series
    assert again.series["5s"].candles(0, 10**15) == live.series["5s"].candles(0, 10**15)


async def test_catch_up_takes_depth_from_the_snapshot_and_later_events(w: World) -> None:
    w.exchange.place(B, "SELL", 5, 24800)
    w.events.snapshots["CALVERA"] = w.exchange.snapshot()  # sequence 1
    w.exchange.place(B, "SELL", 4, 24900)
    fresh = w.new_projection()
    await fresh.start()
    _, asks = fresh.markets["CALVERA"].depth_levels(10)
    assert [(a.price_paise, a.quantity) for a in asks] == [(24800, 5), (24900, 4)]
