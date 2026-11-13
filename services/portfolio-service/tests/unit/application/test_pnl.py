"""Profit and loss (Q-09, Q-10) after real settlements, to the paisa.

unrealised P&L of a holding = (last price − average cost) × quantity; net worth = cash + Σ holding
quantity × last price; P&L = net worth − starting value; P&L % to two decimals.
"""

from datetime import UTC, datetime
from itertools import count
from typing import Any

import pytest

from portfolio_service.application.portfolio import PortfolioService, seeded_trader_id
from portfolio_service.domain.model import AccountKind
from tests.builders import accepted, buy, sell, trade
from tests.fakes import InMemoryPortfolioStore

NOW = datetime(2026, 11, 20, 10, 42, 1, tzinfo=UTC)
BOT03, BOT04 = seeded_trader_id("bot-03"), seeded_trader_id("bot-04")
orders = count(100)


@pytest.fixture
async def service() -> PortfolioService:
    ids = count(1)
    s = PortfolioService(
        InMemoryPortfolioStore(),
        clock=lambda: NOW,
        new_id=lambda: f"3d2a1b0c-0000-4000-8000-{next(ids):012d}",
    )
    await s.seed_accounts()
    return s


async def trade_between(
    s: PortfolioService, buyer: str, seller: str, qty: int, price: int, *, limit: int | None = None
) -> None:
    """A whole order on each side, filled at `price` (the buyer may have offered more)."""
    b, a = next(orders), next(orders)
    await s.create_reservation(buy(b, buyer, qty, limit or price))
    await s.create_reservation(sell(a, seller, qty, price))
    for event in (
        accepted(b, buyer),
        accepted(a, seller),
        trade(b, buyer, a, seller, qty, price, 0, 0),
    ):
        await s.apply_event(event)


def holding(view: dict[str, Any], ticker: str = "CALVERA") -> dict[str, Any]:
    found: list[dict[str, Any]] = [h for h in view["holdings"] if h["ticker"] == ticker]
    [h] = found
    return h


async def test_pnl_follows_buys_price_moves_and_sells_to_the_paisa(
    service: PortfolioService,
) -> None:
    raju, _ = await service.open_account("RajuTrader", AccountKind.HUMAN)
    me = raju.trader_id

    # Buys 20 at 245.50 having offered 246.00: the price improvement comes back.
    await trade_between(service, me, BOT03, 20, 24550, limit=24600)
    v = await service.portfolio(me)
    assert (v["cash_paise"], v["reserved_cash_paise"], v["net_worth_paise"], v["pnl_paise"]) == (
        9_509_000,
        0,
        10_000_000,
        0,
    )
    assert (holding(v)["avg_cost_paise"], holding(v)["unrealised_pnl_paise"]) == (24550, 0)

    # Two bots trade at 246.00: only the valuation moves (the spec's Q-09 example).
    await trade_between(service, BOT04, BOT03, 1, 24600)
    v = await service.portfolio(me)
    assert holding(v)["market_value_paise"] == 492_000
    assert holding(v)["unrealised_pnl_paise"] == 1_000
    assert (v["net_worth_paise"], v["pnl_paise"], v["pnl_percent"]) == (10_001_000, 1_000, 0.01)

    # Buys 30 more at 247.00: average cost (20 × 245.50 + 30 × 247.00) / 50 = 246.40.
    await trade_between(service, me, BOT03, 30, 24700)
    v = await service.portfolio(me)
    h = holding(v)
    assert (h["quantity"], h["avg_cost_paise"], h["last_price_paise"]) == (50, 24640, 24700)
    assert h["unrealised_pnl_paise"] == 50 * (24700 - 24640) == 3_000
    assert v["cash_paise"] == 9_509_000 - 30 * 24700 == 8_768_000
    assert (v["net_worth_paise"], v["pnl_paise"], v["pnl_percent"]) == (10_003_000, 3_000, 0.03)

    # Sells all 50 at 245.00: realised 50 × (245.00 − 246.40) = −700 rupees, nothing held.
    await trade_between(service, BOT04, me, 50, 24500)
    v = await service.portfolio(me)
    assert v["holdings"] == []
    assert (v["cash_paise"], v["net_worth_paise"]) == (9_993_000, 9_993_000)
    assert (v["pnl_paise"], v["pnl_percent"]) == (-7_000, -0.07)


async def test_shares_set_aside_for_an_open_sell_still_count_at_the_last_price(
    service: PortfolioService,
) -> None:
    raju, _ = await service.open_account("RajuTrader", AccountKind.HUMAN)
    await trade_between(service, raju.trader_id, BOT03, 10, 24550)
    await service.create_reservation(sell(next(orders), raju.trader_id, 4, 26000))
    h = holding(await service.portfolio(raju.trader_id))
    assert (h["quantity"], h["reserved_quantity"], h["available_quantity"]) == (10, 4, 6)
    assert h["market_value_paise"] == 10 * 24550


async def test_reserved_cash_is_still_counted_in_net_worth(service: PortfolioService) -> None:
    raju, _ = await service.open_account("RajuTrader", AccountKind.HUMAN)
    await service.create_reservation(buy(next(orders), raju.trader_id, 100, 24600))
    v = await service.portfolio(raju.trader_id)
    assert (v["reserved_cash_paise"], v["available_cash_paise"]) == (2_460_000, 7_540_000)
    assert (v["net_worth_paise"], v["pnl_paise"]) == (10_000_000, 0)


async def test_average_cost_rounds_half_up_and_pnl_uses_it(service: PortfolioService) -> None:
    raju, _ = await service.open_account("RajuTrader", AccountKind.HUMAN)
    await trade_between(service, raju.trader_id, BOT03, 1, 24555)
    await trade_between(service, raju.trader_id, BOT03, 1, 24560)
    h = holding(await service.portfolio(raju.trader_id))
    assert h["avg_cost_paise"] == 24558  # 24557.5 rounds up
    assert h["unrealised_pnl_paise"] == 2 * 24560 - 2 * 24558


async def test_a_bot_pnl_is_measured_against_its_seeded_shares(service: PortfolioService) -> None:
    v = await service.portfolio(BOT03)
    assert v["pnl_paise"] == 0 and v["net_worth_paise"] == v["starting_value_paise"]
    await trade_between(service, BOT04, BOT03, 10, 25550)  # +10 rupees over the reference price
    v = await service.portfolio(BOT03)
    # The sale earned 10 × 255.50 and the last price of the 990 left is now 255.50 as well.
    gained = 10 * 25550 + 990 * 25550 - 1000 * 24550
    assert v["pnl_paise"] == gained == 1_000_000
