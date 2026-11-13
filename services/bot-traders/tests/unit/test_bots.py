"""The ledger, the strategies, the fleet's candle rules and the gateway adapter."""

import asyncio
import json
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from bot_traders.adapters.gateway import GatewayExchange
from bot_traders.application.bot import spread
from bot_traders.application.fleet import Fleet, bot_rng
from bot_traders.domain.ledger import Ledger
from bot_traders.domain.market import (
    Cancel,
    OpenOrder,
    Place,
    Requote,
    buy_protection,
    ceil_tick,
    floor_tick,
    valid_shape,
)
from bot_traders.domain.strategies import (
    MarketMaker,
    MeanReversion,
    Momentum,
    Noise,
    TickerView,
    roster,
)
from bot_traders.ports.exchange import OrderRefusedError, Session
from bot_traders.runtime.events import build_event
from tests.sim import SimExchange, VirtualTime

# ------------------------------------------------------------------ market rules and the ledger


def test_ticks_round_the_right_way_and_never_below_one_tick() -> None:
    assert (floor_tick(24557.9), ceil_tick(24551)) == (24555, 24555)
    assert (floor_tick(1), ceil_tick(0.1)) == (5, 5)
    assert buy_protection(8820) == 9705  # the spec's example: 8820 + 10% rounded up


def test_order_shapes_the_exchange_refuses() -> None:
    assert valid_shape(Place("CALVERA", "BUY", 0, 24550)) == "VALIDATION_FAILED"
    assert valid_shape(Place("CALVERA", "BUY", 10_001, 24550)) == "VALIDATION_FAILED"
    assert valid_shape(Place("CALVERA", "BUY", 1, 24552)) == "INVALID_TICK_SIZE"
    assert valid_shape(Place("NOPE", "BUY", 1, 24550)) == "UNKNOWN_TICKER"
    assert valid_shape(Place("CALVERA", "SELL", 1, None)) is None


def portfolio(cash: int, shares: int = 0) -> dict[str, Any]:
    return {
        "available_cash_paise": cash,
        "holdings": [{"ticker": "CALVERA", "quantity": shares, "available_quantity": shares}],
    }


def test_the_ledger_keeps_placed_orders_aside_until_a_portfolio_read_includes_them() -> None:
    ledger = Ledger()
    ledger.refresh(portfolio(100_000, shares=10), requested_at=0)
    buy = Place("CALVERA", "BUY", 3, 24550)
    assert ledger.refusal(buy, 24550) is None
    ledger.hold(buy, 24550, answered_at=1)
    assert ledger.available_cash() == 100_000 - 73_650
    assert ledger.refusal(Place("CALVERA", "BUY", 2, 24550), 24550) == "INSUFFICIENT_FUNDS"
    ledger.hold(Place("CALVERA", "SELL", 8, 25000), 24550, answered_at=1)
    assert ledger.refusal(Place("CALVERA", "SELL", 3, 25000), 24550) == "INSUFFICIENT_SHARES"
    # A read requested before the answers may not include them: they stay set aside.
    ledger.refresh(portfolio(100_000, shares=10), requested_at=0.5)
    assert ledger.available_cash() == 100_000 - 73_650
    # A read requested after them includes them: the portfolio's own numbers count.
    ledger.refresh(portfolio(26_350, shares=2), requested_at=2)
    assert (ledger.available_cash(), ledger.available_shares("CALVERA")) == (26_350, 2)


def test_a_market_buy_is_costed_above_what_portfolio_reserves() -> None:
    ledger = Ledger()
    cost = ledger.cost(Place("HELIOR", "BUY", 100, None), 8820)
    assert cost > 100 * 9705  # 9705 is what portfolio-service reserves per share


# ------------------------------------------------------------------ strategies


def seeded() -> Any:
    return bot_rng(1, "test")


def view(**fields: Any) -> TickerView:
    base: dict[str, Any] = {
        "ticker": "CALVERA",
        "closes": deque([24550] * 30),
        "best_bid": 24540,
        "best_ask": 24560,
        "candle": 10,
        "shares_held": 1_000,
    }
    return TickerView(**(base | fields))


def quotes(intents: list[Any]) -> list[Place]:
    """The new quotes in a market maker's intents (each sent with what it replaces)."""
    return [i.place for i in intents if isinstance(i, Requote)]


def test_a_market_maker_quotes_both_sides_on_ticks_around_the_price() -> None:
    intents = MarketMaker(half_spread_bps=10).decide(view(), seeded())
    bid, ask = (next(q for q in quotes(intents) if q.side == s) for s in ("BUY", "SELL"))
    assert bid.limit_price_paise is not None and ask.limit_price_paise is not None
    assert bid.limit_price_paise < 24550 < ask.limit_price_paise
    assert (ask.limit_price_paise - bid.limit_price_paise) / 24550 < 0.003
    assert bid.limit_price_paise % 5 == 0 and ask.limit_price_paise % 5 == 0


def test_a_market_maker_keeps_a_good_quote_and_replaces_a_stale_one() -> None:
    mm = MarketMaker(half_spread_bps=10)
    bid = next(q for q in quotes(mm.decide(view(), seeded())) if q.side == "BUY")
    resting = OpenOrder(
        "o1", "CALVERA", "BUY", bid.limit_price_paise, bid.quantity, placed_candle=9
    )
    again = mm.decide(view(open_orders=[resting]), seeded())
    assert not any(q.side == "BUY" for q in quotes(again))  # keeps it
    moved = mm.decide(view(open_orders=[resting], closes=deque([25000] * 30)), seeded())
    [requote] = [i for i in moved if isinstance(i, Requote) and i.place.side == "BUY"]
    # The price moved: a new bid, sent together with the cancel of the old one (no gap).
    assert requote.cancels == (Cancel("o1", "CALVERA"),)
    assert not any(isinstance(i, Cancel) for i in moved)


def test_a_market_maker_long_on_shares_quotes_lower_to_sell_them() -> None:
    mm = MarketMaker(half_spread_bps=10)
    flat = mm.decide(view(shares_held=1_000), seeded())
    long = mm.decide(view(shares_held=1_800), seeded())

    def ask(intents: list[Any]) -> int:
        [price] = [q.limit_price_paise for q in quotes(intents) if q.side == "SELL"]
        assert price is not None
        return price

    assert ask(long) < ask(flat)


def test_a_market_maker_short_of_shares_never_bids_far_above_its_fair_price() -> None:
    # Phase 4: bidding up to 30 bp above fair to buy back shares fed a run-away price on HELIOR.
    mm = MarketMaker(half_spread_bps=8)
    short = mm.decide(view(shares_held=0), seeded())
    [bid] = [q.limit_price_paise for q in quotes(short) if q.side == "BUY"]
    assert bid is not None
    fair = 24550  # last == reference here, so the lean changes nothing
    assert (bid - fair) / fair * 10_000 <= 15 - 8


def test_a_market_makers_fair_price_leans_towards_the_reference() -> None:
    above = view(closes=deque([27000] * 30), best_bid=26990, best_ask=27010)
    [bid, ask] = sorted(
        q.limit_price_paise or 0
        for q in quotes(MarketMaker(half_spread_bps=8).decide(above, seeded()))
    )
    fair = 27000 - 0.05 * (27000 - 24550)  # 26877.5: 5% of the gap every candle
    assert bid < fair < ask < 27000


def test_momentum_needs_three_ticks_on_a_cheap_ticker() -> None:
    # HELIOR is 8820 paise: 8 basis points is 7 paise, under 2 ticks of 5 paise.
    always = Momentum(chance=1.0)
    wiggle = view(ticker="HELIOR", closes=deque([8820] * 10 + [8830]), best_bid=8815, best_ask=8835)
    trend = view(ticker="HELIOR", closes=deque([8820] * 10 + [8835]), best_bid=8830, best_ask=8840)
    assert always.decide(wiggle, seeded()) == []
    assert [i.side for i in always.decide(trend, seeded()) if isinstance(i, Place)] == ["BUY"]


def test_momentum_follows_the_move() -> None:
    rising = view(closes=deque([24000] * 10 + [24550]))
    falling = view(closes=deque([25000] * 10 + [24550]))
    always = Momentum(chance=1.0)
    assert [i.side for i in always.decide(rising, seeded()) if isinstance(i, Place)] == ["BUY"]
    assert [i.side for i in always.decide(falling, seeded()) if isinstance(i, Place)] == ["SELL"]
    assert always.decide(view(), seeded()) == []  # no move, no trade


def test_mean_reversion_leans_against_the_move_with_limit_orders() -> None:
    high = view(closes=deque([24000] * 20 + [24700]))
    [order] = MeanReversion(chance=1.0).decide(high, seeded())
    assert isinstance(order, Place) and order.side == "SELL" and order.limit_price_paise


def test_noise_cancels_its_old_orders() -> None:
    old = OpenOrder("o1", "CALVERA", "BUY", 24500, 5, placed_candle=1)
    intents = Noise(chance=0.0).decide(view(open_orders=[old]), seeded())
    assert intents == [Cancel("o1", "CALVERA")]


def test_the_roster_has_the_documented_mix_of_twenty_bots() -> None:
    plans = roster()
    assert [p.name for p in plans] == [f"bot-{n:02d}" for n in range(1, 21)]
    kinds = [p.strategy.name for p in plans]
    assert (kinds.count("market_maker"), kinds.count("momentum")) == (4, 6)
    assert (kinds.count("mean_reversion"), kinds.count("noise")) == (5, 5)
    assert all(
        set(p.tickers) == {"CALVERA", "KORVIN", "ASTRYX", "HELIOR", "TAVIRA"} for p in plans[:4]
    )  # market makers quote every ticker


def test_orders_are_spread_inside_the_interval_and_a_seed_repeats_them() -> None:
    offsets = spread(50, 5.0, bot_rng(42, "bot-07"))
    assert offsets == sorted(offsets) and all(0.25 <= o <= 4.75 for o in offsets)
    assert offsets == spread(50, 5.0, bot_rng(42, "bot-07"))
    assert offsets != spread(50, 5.0, bot_rng(42, "bot-08"))


# ------------------------------------------------------------------ the fleet's candle rules


def candle(ticker: str, end: datetime, interval: str = "5s", close: int = 24600) -> Any:
    stamp = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    return build_event(
        "CandleClosed",
        {
            "ticker": ticker,
            "interval": interval,
            "bucket_start": stamp,
            "bucket_end": stamp,
            "open_paise": close,
            "high_paise": close,
            "low_paise": close,
            "close_paise": close,
            "volume": 0,
            "trade_count": 0,
        },
        producer="market-data-service",
        environment="dev",
        correlation_id="c",
        event_id=f"c0c0c0c0-0000-4000-8000-{abs(hash((ticker, stamp, interval))) % 10**12:012d}",
    )


async def test_the_fleet_acts_once_per_fresh_5s_candle() -> None:
    now = datetime(2026, 11, 20, 10, 42, tzinfo=UTC)
    clock = VirtualTime()
    fleet = Fleet(
        SimExchange(clock), seed=1, wall_clock=lambda: now, clock=clock.clock, sleep=clock.sleep
    )
    assert await fleet.on_candle(candle("CALVERA", now)) is False  # not started yet
    await fleet.start()
    fresh = candle("CALVERA", now - timedelta(seconds=1))
    assert await fleet.on_candle(fresh) is True
    assert await fleet.on_candle(fresh) is False  # the same event again
    assert await fleet.on_candle(candle("CALVERA", now - timedelta(seconds=30))) is False  # stale
    assert (
        await fleet.on_candle(candle("CALVERA", now, interval="1m")) is False
    )  # not the heartbeat
    assert fleet.market["CALVERA"].closes[-1] == 24600
    fleet.stop()


async def test_market_makers_quote_at_start_up_so_the_first_buyer_finds_a_seller() -> None:
    clock = VirtualTime()
    exchange = SimExchange(clock)
    fleet = Fleet(exchange, seed=1, clock=clock.clock, sleep=clock.sleep)
    await fleet.start()
    await clock.run_until(6)
    await fleet.wait_idle()
    for ticker in ("CALVERA", "KORVIN", "ASTRYX", "HELIOR", "TAVIRA"):
        bid, ask = exchange.best(ticker)
        assert bid is not None and ask is not None and bid < ask


# ------------------------------------------------------------------ the gateway adapter


async def test_the_gateway_adapter_speaks_the_public_api() -> None:
    seen: list[httpx.Request] = []
    limited = {"done": False}

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/v1/bot-sessions":
            assert request.headers["X-Bot-Secret"] == "s3cret"
            return httpx.Response(201, json={"access_token": "tok", "trader_id": "b0b0"})
        if path == "/v1/orders" and request.method == "POST":
            if not limited["done"]:
                limited["done"] = True
                return httpx.Response(429, headers={"Retry-After": "0"})
            body = json.loads(request.content)
            if body["quantity"] == 99:
                return httpx.Response(422, json={"code": "INSUFFICIENT_FUNDS", "detail": "no"})
            return httpx.Response(202, json={"order_id": "o1", "status": "PENDING"})
        if path.startswith("/v1/orders/"):
            return httpx.Response(409, json={"code": "ORDER_NOT_CANCELLABLE"})
        if path == "/v1/orders":
            assert request.url.params["status"] == "open"
            return httpx.Response(200, json={"items": [], "next_cursor": None})
        return httpx.Response(200, json={"instruments": [], "best_bid_paise": None})

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
        gateway = GatewayExchange("http://gateway/", "s3cret", client)
        session = await gateway.login("bot-01")
        assert session == Session("bot-01", "b0b0", "tok")
        placed = await gateway.place(session, Place("CALVERA", "BUY", 5, 24550))
        assert placed["order_id"] == "o1"  # after waiting out one 429
        with pytest.raises(OrderRefusedError) as refused:
            await gateway.place(session, Place("CALVERA", "BUY", 99, None))
        assert refused.value.code == "INSUFFICIENT_FUNDS"
        await gateway.cancel(session, "o1")  # already finished: not an error
        assert await gateway.open_orders(session) == []
    orders = [r for r in seen if r.url.path == "/v1/orders" and r.method == "POST"]
    assert all(r.headers["Authorization"] == "Bearer tok" for r in orders)
    assert (
        len({r.headers["Idempotency-Key"] for r in orders}) == len(orders) - 1
    )  # 429 retry reuses
    market = json.loads(orders[-1].content)
    assert market == {"ticker": "CALVERA", "side": "BUY", "order_type": "MARKET", "quantity": 99}


# ------------------------------------------------------------------ the Q-10 stream


class Stream(SimExchange):
    """A stream that drops once, then works."""

    def __init__(self) -> None:
        super().__init__(VirtualTime())
        self.connects = 0

    async def portfolio_stream(self, session: Session) -> Any:
        self.connects += 1
        if self.connects == 1:
            raise ConnectionError("gateway restarting")
        async for message in super().portfolio_stream(session):
            yield message


async def test_fills_and_closes_on_the_stream_keep_the_open_orders_right() -> None:
    from bot_traders.application.bot import Bot

    bot = Bot(roster()[0], SimExchange(VirtualTime()), seeded())
    bot.open_orders = {
        "a": OpenOrder("a", "CALVERA", "BUY", 24500, 10, 1),
        "b": OpenOrder("b", "CALVERA", "SELL", 24600, 10, 1),
    }
    bot.on_stream({"type": "fill", "data": {"order_id": "a", "order_remaining_quantity": 4}})
    bot.on_stream({"type": "order_closed", "data": {"order_id": "b", "status": "CANCELLED"}})
    bot.on_stream({"type": "portfolio", "data": {}})
    assert list(bot.open_orders) == ["a"] and bot.open_orders["a"].remaining == 4
    bot.on_stream({"type": "fill", "data": {"order_id": "a", "order_remaining_quantity": 0}})
    assert bot.open_orders == {}


async def test_a_close_that_overtakes_the_order_answer_is_not_forgotten() -> None:
    from bot_traders.application.bot import Bot, MarketState

    exchange = SimExchange(VirtualTime())
    bot = Bot(roster()[0], exchange, seeded())
    await bot.login()
    await bot.refresh()
    bot.on_stream(
        {"type": "order_closed", "data": {"order_id": "0dde0000-0000-4000-8000-000000000001"}}
    )
    await bot.execute(Place("CALVERA", "BUY", 1, 24000), MarketState([24550]))
    assert bot.open_orders == {}  # it finished before the A-05 answer came back


async def test_after_the_stream_drops_the_next_refresh_lists_open_orders_again() -> None:
    from bot_traders.application.bot import Bot

    exchange = Stream()
    bot = Bot(roster()[0], exchange, seeded(), clock=exchange.time.clock, sleep=exchange.time.sleep)
    await bot.login()
    await bot.refresh()
    assert exchange.order_lists == 1
    listening = asyncio.create_task(bot.listen())
    await exchange.time.run_until(2)  # dropped once, back after a 1 s back-off
    assert exchange.connects == 2
    exchange.time.now += 10
    await bot.refresh()
    assert exchange.order_lists == 2
    listening.cancel()


class SlowListing(SimExchange):
    """Q-07 times out (order-service saturated) until `healthy` is set."""

    def __init__(self) -> None:
        super().__init__(VirtualTime())
        self.healthy = False
        self.listings_tried = 0

    async def open_orders(self, session: Session) -> list[dict[str, Any]]:
        self.listings_tried += 1
        if not self.healthy:
            raise TimeoutError("ReadTimeout")
        return await super().open_orders(session)


async def test_a_failing_bot_backs_off_instead_of_retrying_every_candle() -> None:
    from bot_traders.application.bot import STEP_BACKOFF_MAX_SECONDS, Bot, MarketState

    exchange = SlowListing()
    bot = Bot(roster()[0], exchange, seeded(), clock=exchange.time.clock, sleep=exchange.time.sleep)
    await bot.login()
    market = MarketState([24550])
    tried_at: list[float] = []
    for second in range(0, 120, 5):  # a candle every 5 s for two minutes
        exchange.time.now = float(second)
        before = exchange.listings_tried
        await bot.act("CALVERA", market)
        if exchange.listings_tried > before:
            tried_at.append(exchange.time.now)
    # 1 s, 2 s, 4 s ... between tries, rounded up to the next candle, never more than 30 s apart.
    assert tried_at[:5] == [0.0, 5.0, 10.0, 15.0, 25.0]
    gaps = [b - a for a, b in zip(tried_at, tried_at[1:], strict=False)]
    assert gaps == sorted(gaps) and max(gaps) <= STEP_BACKOFF_MAX_SECONDS + 5
    assert len(tried_at) < 24 / 2  # far fewer than one try per candle

    exchange.healthy = True  # order-service recovered
    tries = exchange.listings_tried
    exchange.time.now = 1_000.0
    acting = asyncio.create_task(bot.act("CALVERA", market))
    await exchange.time.run_until(1_005.0)  # the step's orders go out over the interval
    await acting
    assert exchange.listings_tried == tries + 1 and bot._backoff == 0.0  # the pause is reset


class Expiring(SimExchange):
    """Sessions that end: after `expire()`, every call with an old token is refused (401)."""

    def __init__(self) -> None:
        super().__init__(VirtualTime())
        self.logins = 0
        self.valid: set[str] = set()

    async def login(self, account: str) -> Session:
        self.logins += 1
        base = await super().login(account)  # opens the account the first time
        session = Session(base.account, base.trader_id, f"token-{account}-{self.logins}")
        self.valid.add(session.token)
        return session

    def expire(self) -> None:
        self.valid.clear()

    async def portfolio(self, session: Session) -> dict[str, Any]:
        from bot_traders.ports.exchange import SessionExpiredError

        if session.token not in self.valid:
            raise SessionExpiredError("401")
        return await super().portfolio(session)


async def test_a_bot_whose_session_expired_signs_in_again_and_carries_on() -> None:
    from bot_traders.application.bot import Bot, MarketState

    exchange = Expiring()
    bot = Bot(roster()[0], exchange, seeded(), clock=exchange.time.clock, sleep=exchange.time.sleep)
    await bot.login()
    market = MarketState([24550])

    async def step(at: float) -> None:
        exchange.time.now = at
        acting = asyncio.create_task(bot.act("CALVERA", market))
        await exchange.time.run_until(at + 5)
        await acting

    await step(0)
    assert exchange.logins == 1 and bot.session is not None and exchange.placed
    exchange.expire()  # twelve hours later
    await step(100)
    assert bot.session is None  # refused: dropped, to sign in on the next step
    lists = exchange.order_lists
    await step(110)
    assert exchange.logins == 2 and bot.session is not None and bot._backoff == 0.0
    assert exchange.order_lists == lists + 1  # what finished while signed out: listed again


async def test_a_requote_places_the_new_quote_before_it_cancels_the_old_one() -> None:
    """The README's promise: a market maker always has a bid and an ask. Replacing a quote never
    leaves its side empty, not even for the moment between the two requests."""
    from bot_traders.application.bot import Bot, MarketState

    exchange = SimExchange(VirtualTime())
    bot = Bot(roster()[0], exchange, seeded())
    await bot.login()
    await bot.refresh()
    market = MarketState([24550])
    await bot.execute(Place("CALVERA", "BUY", 5, 24500), market)
    [old] = list(bot.open_orders)
    calls: list[str] = []
    real_place, real_cancel = exchange.place, exchange.cancel

    async def place(session: Session, order: Place) -> dict[str, Any]:
        calls.append("place")
        return await real_place(session, order)

    async def cancel(session: Session, order_id: str) -> None:
        calls.append("cancel")
        await real_cancel(session, order_id)

    exchange.place, exchange.cancel = place, cancel  # type: ignore[method-assign,assignment]
    await bot.execute(Requote(Place("CALVERA", "BUY", 5, 24510), (Cancel(old, "CALVERA"),)), market)
    assert calls == ["place", "cancel"]
    assert old not in bot.open_orders and len(bot.open_orders) == 1


async def test_a_lost_answer_is_asked_again_with_the_same_key_so_no_order_is_orphaned() -> None:
    tries: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        tries.append(request.headers["Idempotency-Key"])
        if len(tries) == 1:
            raise httpx.ReadTimeout("the answer was lost", request=request)
        return httpx.Response(202, json={"order_id": "o7", "status": "PENDING"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
        gateway = GatewayExchange("http://gateway/", "s3cret", client)
        placed = await gateway.place(
            Session("bot-01", "b0b0", "tok"), Place("KORVIN", "SELL", 3, 118000)
        )
    assert placed["order_id"] == "o7"
    assert (
        len(tries) == 2 and tries[0] == tries[1]
    )  # the same key: order-service returns the same order
