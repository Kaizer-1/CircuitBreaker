"""Use cases A-02, A-06, A-09 and Q-09 against the in-memory store."""

from datetime import UTC, datetime
from itertools import count

import pytest

from portfolio_service.application.portfolio import PortfolioService, seeded_trader_id
from portfolio_service.domain.model import (
    BOT_STARTING_CASH,
    REFERENCE_PRICES,
    Account,
    AccountKind,
    AccountNotFoundError,
    InsufficientFundsError,
    NicknameTakenError,
    ReservationStatus,
    SettlementError,
)
from portfolio_service.runtime.events import build_event
from tests.builders import accepted, buy, cancelled, expired, oid, sell, trade
from tests.fakes import InMemoryPortfolioStore, InMemoryTx

NOW = datetime(2026, 11, 20, 10, 42, 1, 240_000, tzinfo=UTC)
BOT03 = seeded_trader_id("bot-03")


@pytest.fixture
def store() -> InMemoryPortfolioStore:
    return InMemoryPortfolioStore()


@pytest.fixture
def service(store: InMemoryPortfolioStore) -> PortfolioService:
    ids = count(1)
    return PortfolioService(
        store, clock=lambda: NOW, new_id=lambda: f"3d2a1b0c-0000-4000-8000-{next(ids):012d}"
    )


async def human(service: PortfolioService, nickname: str = "RajuTrader") -> str:
    account, _ = await service.open_account(nickname, AccountKind.HUMAN)
    return account.trader_id


# ------------------------------------------------------------------ A-02


async def test_a_human_starts_with_1_lakh_rupees_and_no_shares(service: PortfolioService) -> None:
    account, created = await service.open_account("RajuTrader", AccountKind.HUMAN)
    assert created
    view = await service.portfolio(account.trader_id)
    assert (view["cash_paise"], view["holdings"], view["starting_value_paise"]) == (
        10_000_000,
        [],
        10_000_000,
    )


async def test_a_taken_human_nickname_is_refused(service: PortfolioService) -> None:
    await human(service)
    with pytest.raises(NicknameTakenError):
        await service.open_account("RajuTrader", AccountKind.HUMAN)


async def test_bot_accounts_are_seeded_once_with_cash_and_shares_and_stable_ids(
    service: PortfolioService, store: InMemoryPortfolioStore
) -> None:
    await service.seed_accounts()
    await service.seed_accounts()  # every start-up seeds again: harmless
    assert len(store.state.accounts) == 30
    bot, created = await service.open_account("bot-03", AccountKind.BOT)
    assert (created, bot.trader_id) == (False, "b0b0b0b0-0003-4000-8000-000000000003")
    assert seeded_trader_id("load-07") == "10ad10ad-0007-4000-8000-000000000007"
    view = await service.portfolio(bot.trader_id)
    assert view["cash_paise"] == BOT_STARTING_CASH
    assert {h["ticker"]: h["quantity"] for h in view["holdings"]} == dict.fromkeys(
        REFERENCE_PRICES, 1000
    )
    assert view["starting_value_paise"] == BOT_STARTING_CASH + 1000 * sum(REFERENCE_PRICES.values())
    assert view["pnl_paise"] == 0


async def test_a_bot_name_already_used_by_a_human_is_refused(service: PortfolioService) -> None:
    await service.open_account("bot-ish", AccountKind.HUMAN)
    with pytest.raises(NicknameTakenError):
        await service.open_account("bot-ish", AccountKind.BOT)


async def test_a_bot_seeded_by_another_replica_at_the_same_moment_is_returned(
    service: PortfolioService, monkeypatch: pytest.MonkeyPatch
) -> None:
    await service.open_account("bot-05", AccountKind.BOT)
    real_lookup = InMemoryTx.account_by_nickname
    calls = 0

    async def lookup_that_misses_once(self: InMemoryTx, nickname: str) -> Account | None:
        nonlocal calls
        calls += 1
        return None if calls == 1 else await real_lookup(self, nickname)

    # The first read misses (the other replica has not committed yet); the insert then hits the
    # unique nickname, and the service returns the account the other replica created.
    monkeypatch.setattr(InMemoryTx, "account_by_nickname", lookup_that_misses_once)
    bot, created = await service.open_account("bot-05", AccountKind.BOT)
    assert (created, bot.trader_id, calls) == (False, seeded_trader_id("bot-05"), 2)


# ------------------------------------------------------------------ A-06


async def test_a_reservation_is_idempotent_per_order(service: PortfolioService) -> None:
    raju = await human(service)
    first, created = await service.create_reservation(buy(1, raju, 50, 24600))
    again, created_again = await service.create_reservation(buy(1, raju, 50, 24600))
    assert (created, created_again) == (True, False)
    assert again.reservation_id == first.reservation_id
    assert (await service.portfolio(raju))["reserved_cash_paise"] == 1_230_000  # not twice


async def test_a_market_buy_uses_the_last_traded_price_or_the_reference_price(
    service: PortfolioService,
) -> None:
    raju = await human(service)
    before, _ = await service.create_reservation(buy(1, raju, 10, None, ticker="HELIOR"))
    assert before.protection_price_paise == 9705  # reference price 8820 + 10%
    await service.seed_accounts()
    await service.create_reservation(buy(2, raju, 1, 9000, ticker="HELIOR"))
    await service.create_reservation(sell(3, seeded_trader_id("bot-01"), 1, 9000, ticker="HELIOR"))
    await service.apply_event(
        trade(2, raju, 3, seeded_trader_id("bot-01"), 1, 9000, 0, 0, ticker="HELIOR")
    )
    after, _ = await service.create_reservation(buy(4, raju, 10, None, ticker="HELIOR"))
    assert after.protection_price_paise == 9900  # last price 9000 + 10%


async def test_a_reservation_for_an_unknown_trader_is_not_found(service: PortfolioService) -> None:
    with pytest.raises(AccountNotFoundError):
        await service.create_reservation(buy(1, "00000000-0000-4000-8000-000000000000", 1, 5))


async def test_a_refused_reservation_reserves_nothing(service: PortfolioService) -> None:
    raju = await human(service)
    with pytest.raises(InsufficientFundsError):
        await service.create_reservation(buy(1, raju, 10_000, 24600))
    assert (await service.portfolio(raju))["reserved_cash_paise"] == 0


# ------------------------------------------------------------------ A-09


async def test_a_trade_settles_both_sides_in_one_transaction(
    service: PortfolioService, store: InMemoryPortfolioStore
) -> None:
    await service.seed_accounts()
    raju = await human(service)
    await service.create_reservation(buy(1, raju, 50, 24600))
    await service.create_reservation(sell(2, BOT03, 20, 24550))
    assert await service.apply_event(accepted(1, raju))
    assert await service.apply_event(trade(1, raju, 2, BOT03, 20, 24550, 30, 0))

    me = await service.portfolio(raju)
    assert (me["cash_paise"], me["reserved_cash_paise"]) == (
        10_000_000 - 491_000,
        1_230_000 - 491_000,
    )
    assert [(h["ticker"], h["quantity"], h["avg_cost_paise"]) for h in me["holdings"]] == [
        ("CALVERA", 20, 24550)
    ]
    bot = await service.portfolio(BOT03)
    assert bot["cash_paise"] == BOT_STARTING_CASH + 491_000
    assert {h["ticker"]: h["quantity"] for h in bot["holdings"]}["CALVERA"] == 980
    assert store.state.reservations[oid(1)].confirmed
    assert store.state.reservations[oid(2)].status is ReservationStatus.CONSUMED
    assert len(store.state.settlements) == 2
    assert store.state.last_prices == {"CALVERA": 24550}


async def test_a_duplicate_event_changes_nothing(
    service: PortfolioService, store: InMemoryPortfolioStore
) -> None:
    await service.seed_accounts()
    raju = await human(service)
    await service.create_reservation(buy(1, raju, 50, 24600))
    await service.create_reservation(sell(2, BOT03, 20, 24550))
    event = trade(1, raju, 2, BOT03, 20, 24550, 30, 0)
    assert await service.apply_event(event)
    after_once = (await service.portfolio(raju), await service.portfolio(BOT03))
    assert await service.apply_event(event) is False
    assert (await service.portfolio(raju), await service.portfolio(BOT03)) == after_once


async def test_finishing_an_order_releases_its_price_improvement(service: PortfolioService) -> None:
    await service.seed_accounts()
    raju = await human(service)
    await service.create_reservation(buy(1, raju, 20, 24600))
    await service.create_reservation(sell(2, BOT03, 20, 24550))
    await service.apply_event(trade(1, raju, 2, BOT03, 20, 24550, 0, 0))
    me = await service.portfolio(raju)
    assert (me["cash_paise"], me["reserved_cash_paise"]) == (10_000_000 - 491_000, 0)


@pytest.mark.parametrize(
    "reason", ["USER_REQUESTED", "SELF_TRADE_PREVENTED", "ORDER_EXPIRED", "UNFILLED_MARKET_ORDER"]
)
async def test_a_cancelled_order_releases_what_is_left(
    service: PortfolioService, store: InMemoryPortfolioStore, reason: str
) -> None:
    raju = await human(service)
    await service.create_reservation(buy(1, raju, 50, 24600))
    await service.apply_event(cancelled(1, raju, 50, reason))
    assert (await service.portfolio(raju))["reserved_cash_paise"] == 0
    assert store.state.reservations[oid(1)].status is ReservationStatus.RELEASED


async def test_an_expired_order_releases_its_whole_reservation(
    service: PortfolioService, store: InMemoryPortfolioStore
) -> None:
    raju = await human(service)
    await service.create_reservation(buy(1, raju, 50, 24600))
    await service.apply_event(expired(1, raju))
    assert (await service.portfolio(raju))["available_cash_paise"] == 10_000_000
    assert store.state.reservations[oid(1)].status is ReservationStatus.RELEASED


async def test_a_release_for_an_order_with_nothing_held_does_nothing(
    service: PortfolioService,
) -> None:
    raju = await human(service)
    assert await service.apply_event(cancelled(9, raju, 1))  # never reserved: recorded, no change
    await service.create_reservation(buy(1, raju, 5, 24600))
    await service.apply_event(cancelled(1, raju, 5))
    assert await service.apply_event(expired(1, raju))  # already released
    assert (await service.portfolio(raju))["reserved_cash_paise"] == 0


async def test_a_trade_on_released_money_is_refused_and_rolled_back(
    service: PortfolioService, store: InMemoryPortfolioStore
) -> None:
    await service.seed_accounts()
    raju = await human(service)
    await service.create_reservation(buy(1, raju, 10, 24600))
    await service.create_reservation(sell(2, BOT03, 10, 24600))
    await service.apply_event(expired(1, raju))
    before = await service.portfolio(raju), await service.portfolio(BOT03)
    bad = trade(1, raju, 2, BOT03, 10, 24600, 0, 0)
    with pytest.raises(SettlementError, match="RELEASED"):
        await service.apply_event(bad)
    assert (await service.portfolio(raju), await service.portfolio(BOT03)) == before
    assert bad.event_id not in store.state.processed  # retried by the consumer, then dead-lettered


async def test_a_trade_for_an_unknown_account_is_refused(service: PortfolioService) -> None:
    raju = await human(service)
    with pytest.raises(SettlementError, match="no account"):
        await service.apply_event(trade(1, raju, 2, BOT03, 1, 24600, 0, 0))


async def test_order_accepted_without_a_reservation_is_logged_and_ignored(
    service: PortfolioService,
) -> None:
    assert await service.apply_event(accepted(5, "x"))


async def test_event_types_portfolio_does_not_use_are_ignored(service: PortfolioService) -> None:
    other = build_event(
        "OrderCancelRejected", {}, producer="matching-engine", environment="dev", correlation_id="c"
    )
    assert await service.apply_event(other) is False


async def test_the_portfolio_of_an_unknown_trader_is_not_found(service: PortfolioService) -> None:
    with pytest.raises(AccountNotFoundError):
        await service.portfolio("00000000-0000-4000-8000-000000000000")
