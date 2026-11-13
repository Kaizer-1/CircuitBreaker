"""Consumer contract: portfolio-service reads matching.v1 exactly as the event catalog shows it.

The catalog's examples are applied to real use cases (tolerant reader: unknown fields are ignored).
"""

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from portfolio_service.application.portfolio import PortfolioService, seeded_trader_id
from portfolio_service.domain.model import AccountKind, OrderType, ReservationRequest, Side
from portfolio_service.runtime.events import EventEnvelope
from tests.fakes import InMemoryPortfolioStore

CATALOG = Path(__file__).resolve().parents[4] / "docs" / "03-architecture" / "03-event-catalog.md"
RAJU = "3d2a1b0c-9e8f-4a7b-8c6d-5e4f3a2b1c0d"  # the trader in the catalog's examples


def example(event_type: str) -> EventEnvelope:
    blocks = re.findall(r"```json\n(.*?)\n```", CATALOG.read_text(), re.S)
    [found] = [b for b in map(json.loads, blocks) if b.get("event_type") == event_type]
    return EventEnvelope.model_validate(found | {"added_later_field": True})


async def world() -> tuple[PortfolioService, InMemoryPortfolioStore]:
    store = InMemoryPortfolioStore()
    service = PortfolioService(store)
    await service.open_account("bot-03", AccountKind.BOT)
    account, _ = await service.open_account("RajuTrader", AccountKind.HUMAN)
    store.state.accounts[RAJU] = store.state.accounts.pop(account.trader_id)  # the catalog's ID
    store.state.accounts[RAJU].trader_id = RAJU
    return service, store


def test_the_catalog_examples_settle_a_trade() -> None:
    async def go() -> None:
        service, store = await world()
        trade = example("TradeExecuted")
        d: dict[str, Any] = trade.data
        assert d["seller_trader_id"] == seeded_trader_id("bot-03")
        await service.create_reservation(
            ReservationRequest(
                d["buy_order_id"], RAJU, "CALVERA", Side.BUY, OrderType.LIMIT, 50, 24600
            )
        )
        await service.create_reservation(
            ReservationRequest(
                d["sell_order_id"],
                d["seller_trader_id"],
                "CALVERA",
                Side.SELL,
                OrderType.LIMIT,
                20,
                24550,
            )
        )
        assert await service.apply_event(example("OrderAccepted"))
        assert await service.apply_event(trade)
        view = await service.portfolio(RAJU)
        assert (view["cash_paise"], view["holdings"][0]["quantity"]) == (10_000_000 - 491_000, 20)
        assert await service.apply_event(example("OrderCancelled"))  # the remaining 30 are released
        assert (await service.portfolio(RAJU))["reserved_cash_paise"] == 0

    asyncio.run(go())


def test_the_catalog_example_of_order_expired_releases_a_reservation() -> None:
    async def go() -> None:
        service, _ = await world()
        expired = example("OrderExpired")
        await service.create_reservation(
            ReservationRequest(
                expired.data["order_id"], RAJU, "CALVERA", Side.BUY, OrderType.LIMIT, 5, 24600
            )
        )
        assert await service.apply_event(expired)
        assert (await service.portfolio(RAJU))["reserved_cash_paise"] == 0

    asyncio.run(go())


def test_events_portfolio_does_not_use_are_ignored() -> None:
    async def go() -> None:
        service, _ = await world()
        assert await service.apply_event(example("OrderCancelRejected")) is False

    asyncio.run(go())
