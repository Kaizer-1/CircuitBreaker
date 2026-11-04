"""Q-10: every committed change reaches the replica holding the trader's socket, and only that
trader's socket; nothing is sent for a rollback or a duplicate; price moves re-value holders."""

from datetime import UTC, datetime
from itertools import count
from typing import Any

import pytest

from portfolio_service.application.live import Change, PortfolioHub, Subscriber
from portfolio_service.application.portfolio import PortfolioService, seeded_trader_id
from portfolio_service.domain.model import AccountKind
from tests.builders import accepted, buy, cancelled, expired, oid, sell, trade
from tests.fakes import InMemoryPortfolioStore

NOW = datetime(2026, 11, 20, 10, 42, 1, tzinfo=UTC)
BOT03 = seeded_trader_id("bot-03")


class World:
    def __init__(self) -> None:
        ids = count(1)
        self.store = InMemoryPortfolioStore()
        self.service = PortfolioService(
            self.store,
            clock=lambda: NOW,
            new_id=lambda: f"3d2a1b0c-0000-4000-8000-{next(ids):012d}",
        )
        # Two replicas: each has its own hub, and both LISTEN to every commit.
        self.hubs = [PortfolioHub(self.service.portfolio), PortfolioHub(self.service.portfolio)]
        self.store.listeners += [h.on_change for h in self.hubs]

    async def human(self, nickname: str) -> str:
        account, _ = await self.service.open_account(nickname, AccountKind.HUMAN)
        return account.trader_id


def drain(sub: Subscriber) -> list[dict[str, Any]]:
    out = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait())
    return out


@pytest.fixture
async def w() -> World:
    world = World()
    await world.service.seed_accounts()
    world.store.notified.clear()
    return world


async def test_a_trade_pushes_fill_then_the_new_portfolio_to_that_trader_only(w: World) -> None:
    raju, meera = await w.human("RajuTrader"), await w.human("MeeraMoon")
    raju_sub = w.hubs[1].subscribe(raju)  # Raju's phone is on the *other* replica
    meera_sub = w.hubs[0].subscribe(meera)
    await w.service.create_reservation(buy(1, raju, 20, 24600))
    await w.service.create_reservation(sell(2, BOT03, 50, 24550))
    drain(raju_sub)
    await w.service.apply_event(accepted(1, raju))
    await w.service.apply_event(trade(1, raju, 2, BOT03, 20, 24550, 0, 30))

    kinds = [m["type"] for m in drain(raju_sub)]
    assert kinds == ["fill", "order_closed", "portfolio"]
    assert drain(meera_sub) == []  # never another trader's stream


async def test_fill_and_order_closed_carry_the_api_readme_fields(w: World) -> None:
    raju = await w.human("RajuTrader")
    sub = w.hubs[0].subscribe(raju)
    await w.service.create_reservation(buy(1, raju, 50, 24600))
    await w.service.create_reservation(sell(2, BOT03, 20, 24550))
    await w.service.apply_event(trade(1, raju, 2, BOT03, 20, 24550, 30, 0))
    fill, portfolio = drain(sub)[-2:]
    assert fill == {
        "type": "fill",
        "data": {
            "order_id": oid(1),
            "trade_id": fill["data"]["trade_id"],
            "ticker": "CHAIWALA",
            "side": "BUY",
            "price_paise": 24550,
            "quantity": 20,
            "order_remaining_quantity": 30,
            "executed_at": "2026-11-20T10:42:01.262Z",
        },
    }
    assert portfolio["type"] == "portfolio" and portfolio["data"]["holdings"][0]["quantity"] == 20
    await w.service.apply_event(cancelled(1, raju, 30, "SELF_TRADE_PREVENTED"))
    closed = drain(sub)[0]
    assert closed == {
        "type": "order_closed",
        "data": {
            "order_id": oid(1),
            "status": "CANCELLED",
            "cancel_reason": "SELF_TRADE_PREVENTED",
        },
    }


async def test_an_expiry_releases_money_without_closing_an_order_on_the_phone(w: World) -> None:
    raju = await w.human("RajuTrader")
    sub = w.hubs[0].subscribe(raju)
    await w.service.create_reservation(buy(1, raju, 10, 24600))
    drain(sub)
    await w.service.apply_event(expired(1, raju))
    [message] = drain(sub)
    assert message["type"] == "portfolio" and message["data"]["reserved_cash_paise"] == 0


async def test_a_reservation_pushes_the_reserved_cash(w: World) -> None:
    raju = await w.human("RajuTrader")
    sub = w.hubs[0].subscribe(raju)
    await w.service.create_reservation(buy(1, raju, 10, 24600))
    [message] = drain(sub)
    assert message["data"]["reserved_cash_paise"] == 246_000


async def test_nothing_is_sent_for_a_duplicate_event_or_a_rolled_back_change(w: World) -> None:
    raju = await w.human("RajuTrader")
    await w.service.create_reservation(buy(1, raju, 20, 24600))
    await w.service.create_reservation(sell(2, BOT03, 20, 24550))
    event = trade(1, raju, 2, BOT03, 20, 24550, 0, 0)
    await w.service.apply_event(event)
    sent = len(w.store.notified)
    await w.service.apply_event(event)  # redelivered
    assert len(w.store.notified) == sent

    def fail() -> None:
        raise ConnectionError("commit failed")

    w.store.before_commit = fail
    with pytest.raises(ConnectionError):
        await w.service.create_reservation(buy(3, raju, 1, 24600))
    assert len(w.store.notified) == sent


async def test_a_price_move_revalues_only_holders_and_only_once_per_second(w: World) -> None:
    raju, meera = await w.human("RajuTrader"), await w.human("MeeraMoon")
    await w.service.create_reservation(buy(1, raju, 5, 24600))
    await w.service.create_reservation(sell(2, BOT03, 5, 24600))
    await w.service.apply_event(trade(1, raju, 2, BOT03, 5, 24600, 0, 0))
    raju_sub, meera_sub = w.hubs[0].subscribe(raju), w.hubs[0].subscribe(meera)
    await w.hubs[0].view(raju_sub)  # what the socket sends on connect
    await w.hubs[0].view(meera_sub)
    bot04 = seeded_trader_id("bot-04")
    for n in range(3):  # three trades between bots move CHAIWALA's price
        await w.service.create_reservation(buy(10 + n, bot04, 1, 24700 + n * 5))
        await w.service.create_reservation(sell(20 + n, BOT03, 1, 24700 + n * 5))
        await w.service.apply_event(trade(10 + n, bot04, 20 + n, BOT03, 1, 24700 + n * 5, 0, 0))
    assert drain(raju_sub) == [] and drain(meera_sub) == []  # nothing until the 1 s tick
    await w.hubs[0].revalue()
    [message] = drain(raju_sub)
    assert message["data"]["holdings"][0]["last_price_paise"] == 24710
    assert drain(meera_sub) == []  # Meera holds no CHAIWALA
    await w.hubs[0].revalue()
    assert drain(raju_sub) == []  # no move since: nothing to send


async def test_after_a_lost_listen_connection_every_socket_is_refreshed(w: World) -> None:
    raju = await w.human("RajuTrader")
    sub = w.hubs[0].subscribe(raju)
    await w.hubs[0].refresh_all()
    assert [m["type"] for m in drain(sub)] == ["portfolio"]


async def test_a_change_round_trips_through_its_json_payload() -> None:
    change = Change("t", "CHAIWALA", ({"type": "fill", "data": {"x": 1}},))
    assert Change.from_json(change.to_json()) == change
    assert len(change.to_json()) < 8000  # Postgres NOTIFY's payload limit
