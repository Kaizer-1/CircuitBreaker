"""Integration tests: the use cases on a real Postgres 16 (testcontainers).

Run with `uv run pytest -m integration` (Docker needed). Not part of the default `pytest` run.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import Any

import psycopg
import pytest
from testcontainers.community.postgres import PostgresContainer

from portfolio_service.adapters.postgres import PostgresPortfolioStore
from portfolio_service.application.portfolio import PortfolioService, seeded_trader_id
from portfolio_service.domain.model import (
    AccountKind,
    InsufficientFundsError,
    NicknameTakenError,
    ReservationStatus,
)
from tests.builders import accepted, buy, cancelled, oid, sell, trade

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def conninfo() -> Iterator[str]:
    with PostgresContainer("postgres:16.15", driver=None) as pg:
        yield pg.get_connection_url()


@pytest.fixture
async def store(conninfo: str) -> AsyncIterator[PostgresPortfolioStore]:
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    s = await PostgresPortfolioStore.open(conninfo, max_size=5)
    await s.migrate()
    await s.migrate()  # applying twice is a no-op
    yield s
    await s.close()


async def test_the_full_flow_reserve_accept_settle_and_dedupe(
    store: PostgresPortfolioStore,
) -> None:
    service = PortfolioService(store)
    await service.seed_accounts()
    raju = (await service.open_account("RajuTrader", AccountKind.HUMAN))[0].trader_id
    bot = seeded_trader_id("bot-03")
    await service.create_reservation(buy(1, raju, 50, 24600))
    await service.create_reservation(sell(2, bot, 20, 24550))
    await service.apply_event(accepted(1, raju))
    event = trade(1, raju, 2, bot, 20, 24550, 30, 0)
    assert await service.apply_event(event)
    assert await service.apply_event(event) is False  # the duplicate is dropped by processed_events
    me = await service.portfolio(raju)
    assert (me["cash_paise"], me["reserved_cash_paise"]) == (9_509_000, 739_000)
    assert me["holdings"][0]["quantity"] == 20
    await service.apply_event(cancelled(1, raju, 30))
    assert (await service.portfolio(raju))["reserved_cash_paise"] == 0
    async with store.transaction() as tx:
        r = await tx.reservation(oid(1))
        assert r is not None and (r.status, r.confirmed) == (ReservationStatus.RELEASED, True)


async def test_concurrent_reservations_cannot_spend_the_same_rupees(
    store: PostgresPortfolioStore,
) -> None:
    service = PortfolioService(store)
    raju = (await service.open_account("RajuTrader", AccountKind.HUMAN))[0].trader_id
    # 10 orders of 50 x 24000 = 12,00,000 paise race for 1,00,00,000: row locks let only 8 fit.
    results = await asyncio.gather(
        *(service.create_reservation(buy(n, raju, 50, 24000)) for n in range(1, 11)),
        return_exceptions=True,
    )
    refused = [r for r in results if isinstance(r, InsufficientFundsError)]
    assert len(refused) == 2
    view = await service.portfolio(raju)
    assert view["reserved_cash_paise"] == 8 * 1_200_000
    assert view["available_cash_paise"] == 10_000_000 - 8 * 1_200_000


async def test_the_database_itself_refuses_a_negative_balance(
    store: PostgresPortfolioStore,
) -> None:
    service = PortfolioService(store)
    raju = (await service.open_account("RajuTrader", AccountKind.HUMAN))[0].trader_id
    with pytest.raises(psycopg.errors.CheckViolation):
        async with store.transaction() as tx:
            account = await tx.account(raju)
            assert account is not None
            account.reserved_cash_paise = account.cash_paise + 1  # would be margin
            await tx.save_account(account)


async def test_orphan_candidates_and_the_human_nickname_rule(store: PostgresPortfolioStore) -> None:
    service = PortfolioService(store)
    raju = (await service.open_account("RajuTrader", AccountKind.HUMAN))[0].trader_id
    await service.create_reservation(buy(1, raju, 1, 24600))
    async with store.transaction() as tx:
        assert [
            r.order_id for r in await tx.unconfirmed_reservations("2999-01-01T00:00:00.000Z")
        ] == [oid(1)]
        assert await tx.unconfirmed_reservations("2000-01-01T00:00:00.000Z") == []
    with pytest.raises(NicknameTakenError):
        await service.open_account("RajuTrader", AccountKind.HUMAN)


async def test_every_replica_hears_a_change_only_after_it_commits(
    store: PostgresPortfolioStore, conninfo: str
) -> None:
    from portfolio_service.adapters.postgres_listener import PostgresListener
    from portfolio_service.application.live import Change

    heard: list[list[Change]] = [[], []]

    def recorder(n: int) -> Any:
        async def on_change(raw: str) -> None:
            heard[n].append(Change.from_json(raw))

        return on_change

    async def nothing() -> None:
        return None

    listeners = [PostgresListener(conninfo, recorder(n), nothing) for n in (0, 1)]
    tasks = [asyncio.create_task(listener.run()) for listener in listeners]
    try:
        for _ in range(100):  # until both replicas are listening
            if all(listener.listening for listener in listeners):
                break
            await asyncio.sleep(0.05)
        service = PortfolioService(store)
        raju = (await service.open_account("RajuTrader", AccountKind.HUMAN))[0].trader_id
        with pytest.raises(RuntimeError):
            async with store.transaction() as tx:
                await tx.notify(Change(raju).to_json())
                raise RuntimeError("rolled back")
        await service.create_reservation(buy(1, raju, 10, 24600))
        for _ in range(100):
            if all(heard):
                break
            await asyncio.sleep(0.05)
        assert heard[0] == heard[1] == [Change(raju)]  # one NOTIFY: the committed one
    finally:
        for task in tasks:
            task.cancel()


async def test_the_leaderboard_reads_humans_with_their_shares_and_marks_them_to_market(
    store: PostgresPortfolioStore,
) -> None:
    service = PortfolioService(store)
    await service.seed_accounts()  # 20 bots with shares, 10 load accounts with cash only
    meera = (await service.open_account("Meera", AccountKind.HUMAN))[0].trader_id
    await service.open_account("Raju", AccountKind.HUMAN)
    bot = seeded_trader_id("bot-03")
    await service.create_reservation(buy(1, meera, 40, 24600))
    await service.create_reservation(sell(2, bot, 40, 24550))
    await service.apply_event(accepted(1, meera))
    await service.apply_event(trade(1, meera, 2, bot, 40, 24550, 0, 0))

    async with store.transaction() as tx:
        humans = await tx.standings([AccountKind.HUMAN])
        everyone = await tx.standings([AccountKind.HUMAN, AccountKind.BOT])
    assert sorted(a.nickname for a, _ in humans) == ["Meera", "Raju"]
    held = {a.nickname: [(h.ticker, h.quantity) for h in hs] for a, hs in humans}
    assert held == {"Meera": [("CHAIWALA", 40)], "Raju": []}
    assert sum(a.kind is AccountKind.BOT for a, _ in everyone) == 20
    assert all(a.kind is not AccountKind.LOAD for a, _ in everyone)

    board = await service.leaderboard.get(include_bots=False, limit=20)
    assert [(e["rank"], e["nickname"]) for e in board["entries"]] == [(1, "Meera"), (1, "Raju")]
