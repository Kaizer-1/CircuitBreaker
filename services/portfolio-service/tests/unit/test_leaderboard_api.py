"""Q-11 over HTTP: who is ranked, live updates on price moves, the 2-second cache, the schema."""

import asyncio
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from portfolio_service.application.leaderboard import Leaderboard
from portfolio_service.application.portfolio import PortfolioService
from portfolio_service.config import Settings
from portfolio_service.domain.model import AccountKind
from portfolio_service.main import create_app
from tests.builders import accepted, buy, sell, trade
from tests.fakes import InMemoryPortfolioStore

API = Path(__file__).resolve().parents[4] / "api"
SPEC_URI = "https://circuitbreaker.local/api/portfolio-service.yaml"
COMMON_URI = "https://circuitbreaker.local/api/shared/common.yaml"


@cache
def registry() -> Registry[Any]:
    text = (API / "portfolio-service.yaml").read_text()
    spec = yaml.safe_load(text.replace("./shared/common.yaml#", f"{COMMON_URI}#"))
    common = yaml.safe_load((API / "shared" / "common.yaml").read_text())
    return Registry().with_resources(
        [
            (SPEC_URI, Resource.from_contents(spec, DRAFT202012)),
            (COMMON_URI, Resource.from_contents(common, DRAFT202012)),
        ]
    )


def check_leaderboard(body: Any) -> None:
    validator = Draft202012Validator(
        {"$ref": f"{SPEC_URI}#/components/schemas/Leaderboard"},
        registry=registry(),
        format_checker=FormatChecker(),
    )
    errors = [f"{list(e.absolute_path)}: {e.message}" for e in validator.iter_errors(body)]
    assert errors == [], errors


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def service() -> PortfolioService:
    service = PortfolioService(InMemoryPortfolioStore())
    asyncio.run(service.seed_accounts())  # 20 bots and 10 load accounts
    return service


@pytest.fixture
def monotonic(service: PortfolioService) -> Clock:
    clock = Clock()
    service.leaderboard = Leaderboard(service._store, monotonic=clock)
    return clock


@pytest.fixture
def client(settings: Settings, service: PortfolioService, monotonic: Clock) -> Iterator[TestClient]:
    with TestClient(create_app(settings, readiness_checks=[], service=service)) as c:
        yield c


def join(service: PortfolioService, nickname: str) -> str:
    account, _ = asyncio.run(service.open_account(nickname, AccountKind.HUMAN))
    return account.trader_id


def board(client: TestClient, **params: Any) -> Any:
    response = client.get("/v1/leaderboard", params=params)
    assert response.status_code == 200
    body = response.json()
    check_leaderboard(body)
    return body


def buy_from_a_bot(service: PortfolioService, trader: str, n: int, qty: int, price: int) -> None:
    """The trader buys `qty` CALVERA at `price` from bot-01 (reserve, accept, trade)."""
    bot = next(a for a in asyncio.run(_accounts(service)) if a.nickname == "bot-01").trader_id
    asyncio.run(service.create_reservation(buy(n, trader, qty, price)))
    asyncio.run(service.create_reservation(sell(n + 1, bot, qty, price)))
    for event in (
        accepted(n, trader),
        accepted(n + 1, bot),
        trade(n, trader, n + 1, bot, qty, price, 0, 0),
    ):
        asyncio.run(service.apply_event(event))


async def _accounts(service: PortfolioService) -> list[Any]:
    async with service._store.transaction() as tx:
        return [a for a, _ in await tx.standings(list(AccountKind))]


def test_only_humans_are_ranked_by_default_and_load_accounts_never(
    client: TestClient, service: PortfolioService
) -> None:
    join(service, "Meera")
    join(service, "Raju")
    humans = board(client)
    assert {e["kind"] for e in humans["entries"]} == {"HUMAN"}
    assert [e["rank"] for e in humans["entries"]] == [1, 1]  # both still at ₹1,00,000
    everyone = board(client, include_bots="true", limit=100)
    kinds = [e["kind"] for e in everyone["entries"]]
    assert kinds.count("BOT") == 20 and kinds.count("HUMAN") == 2
    assert "LOAD" not in kinds


def test_a_price_move_changes_the_board_on_the_next_refresh(
    client: TestClient, service: PortfolioService, monotonic: Clock
) -> None:
    meera, raju = join(service, "Meera"), join(service, "Raju")
    buy_from_a_bot(service, meera, 1, 100, 24_500)  # Meera holds 100 CALVERA bought at ₹245.00
    first = board(client)
    assert [(e["rank"], e["nickname"]) for e in first["entries"]] == [(1, "Meera"), (1, "Raju")]

    buy_from_a_bot(service, raju, 10, 1, 26_000)  # a trade moves the last price to ₹260.00
    assert board(client) == first  # within 2 s: the same answer, same as_of
    monotonic.now += 2.0
    moved = board(client)
    assert [(e["rank"], e["nickname"]) for e in moved["entries"]] == [(1, "Meera"), (2, "Raju")]
    top = moved["entries"][0]
    assert top["net_worth_paise"] == 10_000_000 - 100 * 24_500 + 100 * 26_000
    assert top["pnl_percent"] == 1.5
    assert moved["as_of"] >= first["as_of"]
    assert raju != meera


def test_the_limit_is_checked_and_applied(client: TestClient, service: PortfolioService) -> None:
    for n in range(5):
        join(service, f"trader_{n}")
    assert len(board(client, limit=3)["entries"]) == 3
    assert len(board(client)["entries"]) == 5  # default 20
    assert client.get("/v1/leaderboard", params={"limit": 0}).status_code == 422
    assert client.get("/v1/leaderboard", params={"limit": 101}).status_code == 422


def test_callers_arriving_together_share_one_computation(service: PortfolioService) -> None:
    computed = 0
    real = service._store.transaction

    def counting() -> Any:
        nonlocal computed
        computed += 1
        return real()

    service._store.transaction = counting  # type: ignore[method-assign]
    leaderboard = Leaderboard(service._store)

    async def many() -> None:
        await asyncio.gather(*(leaderboard.get(include_bots=False, limit=20) for _ in range(10)))

    asyncio.run(many())
    assert computed == 1


def test_while_starting_the_answer_is_503(settings: Settings) -> None:
    with TestClient(create_app(settings, readiness_checks=[])) as client:
        response = client.get("/v1/leaderboard")
    assert (response.status_code, response.json()["code"]) == (503, "DEPENDENCY_UNAVAILABLE")
