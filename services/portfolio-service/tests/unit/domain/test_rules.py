"""The money rules (no margin, no short selling, exact paise)."""

import pytest

from portfolio_service.domain.model import (
    Account,
    AccountKind,
    Holding,
    InsufficientFundsError,
    InsufficientSharesError,
    InvalidReservationError,
    OrderType,
    Reservation,
    ReservationRequest,
    ReservationStatus,
    SettlementError,
    Side,
)
from portfolio_service.domain.rules import (
    portfolio_view,
    protection_price,
    release,
    reserve,
    round_half_up,
    settle_buy,
    settle_sell,
)

T0 = "2026-11-20T10:42:01.240Z"
RAJU = "3d2a1b0c-9e8f-4a7b-8c6d-5e4f3a2b1c0d"


def account(cash: int = 10_000_000, reserved: int = 0) -> Account:
    return Account(RAJU, "RajuTrader", AccountKind.HUMAN, cash, reserved, 10_000_000, T0)


def request(
    side: str = "BUY", kind: str = "LIMIT", qty: int = 50, price: int | None = 24600
) -> ReservationRequest:
    return ReservationRequest(
        "7f3c9e10-2b4d-4e6f-8a1b-3c5d7e9f0a12",
        RAJU,
        "CHAIWALA",
        Side(side),
        OrderType(kind),
        qty,
        price,
    )


def reserved_for(req: ReservationRequest, a: Account, h: Holding, last: int = 24550) -> Reservation:
    return reserve(req, a, h, last_price_paise=last, reservation_id="r-1", now=T0)


@pytest.mark.parametrize(
    ("last", "side", "expected"),
    [
        (8820, "BUY", 9705),
        (8820, "SELL", 7935),
        (24550, "BUY", 27005),
        (24550, "SELL", 22095),
        (5, "SELL", 5),
    ],
)
def test_the_protection_price_is_last_price_plus_or_minus_10_percent_on_the_tick(
    last: int, side: str, expected: int
) -> None:
    # 8820 -> 9705 is the spec's example (api/portfolio-service.yaml, marketBuy).
    assert protection_price(last, Side(side)) == expected
    assert expected % 5 == 0


def test_round_half_up() -> None:
    assert (round_half_up(5, 2), round_half_up(7, 3), round_half_up(8, 3)) == (3, 2, 3)


def test_a_limit_buy_reserves_quantity_times_limit_price() -> None:
    a = account()
    r = reserved_for(request(), a, Holding(RAJU, "CHAIWALA"))
    assert (r.reserved_cash_paise, r.remaining_cash_paise, r.protection_price_paise) == (
        1_230_000,
        1_230_000,
        None,
    )
    assert a.reserved_cash_paise == 1_230_000
    assert a.available_cash_paise == 10_000_000 - 1_230_000
    assert r.status is ReservationStatus.ACTIVE and not r.confirmed


def test_a_market_buy_reserves_at_the_protection_price() -> None:
    a = account()
    r = reserved_for(
        request(kind="MARKET", qty=100, price=None), a, Holding(RAJU, "MONSOON"), last=8820
    )
    assert (r.protection_price_paise, r.reserved_cash_paise) == (
        9705,
        970_500,
    )  # the spec's example


def test_no_margin_a_buy_needing_more_than_the_available_cash_is_refused() -> None:
    a = account(cash=950_000)
    with pytest.raises(InsufficientFundsError) as refused:
        reserved_for(request(), a, Holding(RAJU, "CHAIWALA"))
    assert (refused.value.needed, refused.value.available) == (1_230_000, 950_000)
    assert a.reserved_cash_paise == 0


def test_reserved_cash_is_not_available_twice() -> None:
    a = account(cash=2_000_000)
    reserved_for(request(), a, Holding(RAJU, "CHAIWALA"))
    with pytest.raises(InsufficientFundsError):
        reserved_for(request(), a, Holding(RAJU, "CHAIWALA"))


def test_a_sell_reserves_shares_and_short_selling_is_refused() -> None:
    h = Holding(RAJU, "CHAIWALA", quantity=20)
    r = reserved_for(request(side="SELL", qty=15), account(), h)
    assert (r.reserved_quantity, r.reserved_cash_paise, h.reserved_quantity) == (15, 0, 15)
    with pytest.raises(InsufficientSharesError) as refused:
        reserved_for(request(side="SELL", qty=6), account(), h)
    assert (refused.value.needed, refused.value.available) == (6, 5)


def test_a_limit_order_needs_a_price_and_a_market_order_must_not_have_one() -> None:
    with pytest.raises(InvalidReservationError):
        reserved_for(request(price=None), account(), Holding(RAJU, "CHAIWALA"))
    with pytest.raises(InvalidReservationError):
        reserved_for(request(kind="MARKET", price=24600), account(), Holding(RAJU, "CHAIWALA"))


def test_a_buy_with_price_improvement_releases_the_leftover_when_it_finishes() -> None:
    # Sequence diagram 3: reserved 50 x 24600, filled 20 @ 24550 then 30 @ 24600.
    a, h = account(), Holding(RAJU, "CHAIWALA")
    r = reserved_for(request(), a, h)
    settle_buy(r, a, h, order_id=r.order_id, quantity=20, price_paise=24550, order_remaining=30)
    assert (a.cash_paise, a.reserved_cash_paise, h.quantity, h.avg_cost_paise) == (
        10_000_000 - 491_000,
        1_230_000 - 491_000,
        20,
        24550,
    )
    settle_buy(r, a, h, order_id=r.order_id, quantity=30, price_paise=24600, order_remaining=0)
    assert a.cash_paise == 10_000_000 - 491_000 - 738_000
    assert a.reserved_cash_paise == 0  # the 1,000 paise of price improvement came back
    assert (h.quantity, h.avg_cost_paise) == (50, 24580)
    assert (r.status, r.remaining_cash_paise) == (ReservationStatus.CONSUMED, 0)


def test_a_sell_turns_reserved_shares_into_cash() -> None:
    a, h = account(cash=0), Holding(RAJU, "CHAIWALA", quantity=20, avg_cost_paise=24000)
    r = reserved_for(request(side="SELL", qty=20), a, h)
    settle_sell(r, a, h, order_id=r.order_id, quantity=20, price_paise=24550, order_remaining=0)
    assert (a.cash_paise, h.quantity, h.reserved_quantity, h.avg_cost_paise) == (491_000, 0, 0, 0)
    assert r.status is ReservationStatus.CONSUMED


def test_a_partial_sell_keeps_the_average_cost() -> None:
    a, h = account(cash=0), Holding(RAJU, "CHAIWALA", quantity=20, avg_cost_paise=24000)
    r = reserved_for(request(side="SELL", qty=20), a, h)
    settle_sell(r, a, h, order_id=r.order_id, quantity=5, price_paise=24550, order_remaining=15)
    assert (h.quantity, h.reserved_quantity, h.avg_cost_paise, r.remaining_quantity) == (
        15,
        15,
        24000,
        15,
    )


def test_release_returns_what_is_left_and_empties_the_reservation() -> None:
    a, h = account(), Holding(RAJU, "CHAIWALA")
    r = reserved_for(request(), a, h)
    assert release(r, a, h) == 1_230_000
    assert (a.reserved_cash_paise, r.remaining_cash_paise) == (0, 0)


def test_a_trade_on_money_that_is_not_reserved_is_refused() -> None:
    a, h = account(), Holding(RAJU, "CHAIWALA")
    r = reserved_for(request(), a, h)
    with pytest.raises(SettlementError, match="costs"):
        settle_buy(r, a, h, order_id=r.order_id, quantity=51, price_paise=24600, order_remaining=0)
    r.status = ReservationStatus.RELEASED
    with pytest.raises(SettlementError, match="RELEASED"):
        settle_buy(r, a, h, order_id=r.order_id, quantity=1, price_paise=24600, order_remaining=49)
    with pytest.raises(SettlementError, match="missing"):
        settle_buy(None, a, h, order_id="x", quantity=1, price_paise=24600, order_remaining=0)
    s = reserved_for(request(side="SELL", qty=5), a, Holding(RAJU, "CHAIWALA", quantity=5))
    with pytest.raises(SettlementError, match="sells"):
        settle_sell(
            s,
            a,
            Holding(RAJU, "CHAIWALA", 5, 5),
            order_id="y",
            quantity=6,
            price_paise=1,
            order_remaining=0,
        )


def test_the_portfolio_view_matches_the_spec_example() -> None:
    # api/portfolio-service.yaml, getMyPortfolio example.
    a = Account(RAJU, "RajuTrader", AccountKind.HUMAN, 9_509_000, 738_000, 10_000_000, T0)
    holdings = [Holding(RAJU, "CHAIWALA", 20, 0, 24550), Holding(RAJU, "JUGAAD")]  # JUGAAD: empty
    view = portfolio_view(
        a, holdings, {"CHAIWALA": 24600, "JUGAAD": 118000}, "2026-11-20T10:42:05.000Z"
    )
    assert view == {
        "trader_id": RAJU,
        "nickname": "RajuTrader",
        "kind": "HUMAN",
        "cash_paise": 9_509_000,
        "reserved_cash_paise": 738_000,
        "available_cash_paise": 8_771_000,
        "holdings": [
            {
                "ticker": "CHAIWALA",
                "quantity": 20,
                "reserved_quantity": 0,
                "available_quantity": 20,
                "avg_cost_paise": 24550,
                "last_price_paise": 24600,
                "market_value_paise": 492_000,
                "unrealised_pnl_paise": 1_000,
            }
        ],
        "net_worth_paise": 10_001_000,
        "starting_value_paise": 10_000_000,
        "pnl_paise": 1_000,
        "pnl_percent": 0.01,
        "as_of": "2026-11-20T10:42:05.000Z",
    }
