"""The money rules: reserve (A-06), settle and release (A-09), and value a portfolio (Q-09).

Pure functions over the Account aggregate. They change the objects they are given; the application
saves them in one transaction. No margin (never reserve more cash than is available) and no short
selling (never reserve more shares than are available).
"""

from typing import Any

from portfolio_service.domain.model import (
    TICK_SIZE_PAISE,
    Account,
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


def protection_price(last_price_paise: int, side: Side) -> int:
    """A MARKET order's worst allowed price: last price +10% (BUY) or -10% (SELL), on the tick.

    Rounded away from the last price (up for BUY, down for SELL) so the band is at least 10%:
    last price 8820 gives 9705 for a BUY (8820 x 1.1 = 9702, up to the next tick).
    """
    if side is Side.BUY:
        return -(-(last_price_paise * 110) // (100 * TICK_SIZE_PAISE)) * TICK_SIZE_PAISE
    return max(last_price_paise * 90 // (100 * TICK_SIZE_PAISE) * TICK_SIZE_PAISE, TICK_SIZE_PAISE)


def round_half_up(numerator: int, denominator: int) -> int:
    """Integer division rounded half up (ADR-0012), for non-negative values."""
    return (2 * numerator + denominator) // (2 * denominator)


def reserve(
    request: ReservationRequest,
    account: Account,
    holding: Holding,
    *,
    last_price_paise: int,
    reservation_id: str,
    now: str,
) -> Reservation:
    """Set aside cash (BUY) or shares (SELL) for one order, or refuse."""
    if request.order_type is OrderType.LIMIT:
        if request.limit_price_paise is None:
            raise InvalidReservationError("a LIMIT order needs limit_price_paise")
        price, protection = request.limit_price_paise, None
    else:
        if request.limit_price_paise is not None:
            raise InvalidReservationError("a MARKET order has no limit_price_paise")
        price = protection = protection_price(last_price_paise, request.side)

    cash = shares = 0
    if request.side is Side.BUY:
        cash = request.quantity * price
        if cash > account.available_cash_paise:
            raise InsufficientFundsError(cash, account.available_cash_paise)
        account.reserved_cash_paise += cash
    else:
        shares = request.quantity
        if shares > holding.available_quantity:
            raise InsufficientSharesError(request.ticker, shares, holding.available_quantity)
        holding.reserved_quantity += shares

    return Reservation(
        reservation_id=reservation_id,
        order_id=request.order_id,
        trader_id=request.trader_id,
        ticker=request.ticker,
        side=request.side,
        order_type=request.order_type,
        quantity=request.quantity,
        limit_price_paise=request.limit_price_paise,
        protection_price_paise=protection,
        reserved_cash_paise=cash,
        reserved_quantity=shares,
        remaining_cash_paise=cash,
        remaining_quantity=shares,
        status=ReservationStatus.ACTIVE,
        confirmed=False,
        expiry_requested_at=None,
        created_at=now,
    )


def release(reservation: Reservation, account: Account, holding: Holding) -> int:
    """Hand back whatever the reservation still holds. Returns the paise or shares released."""
    released = reservation.remaining_cash_paise or reservation.remaining_quantity
    account.reserved_cash_paise -= reservation.remaining_cash_paise
    holding.reserved_quantity -= reservation.remaining_quantity
    reservation.remaining_cash_paise = reservation.remaining_quantity = 0
    return released


def _active(reservation: Reservation | None, order_id: str) -> Reservation:
    if reservation is None or reservation.status is not ReservationStatus.ACTIVE:
        state = "missing" if reservation is None else reservation.status
        raise SettlementError(f"order {order_id}: reservation is {state}, cannot settle a trade")
    return reservation


def settle_buy(
    reservation: Reservation | None,
    account: Account,
    holding: Holding,
    *,
    order_id: str,
    quantity: int,
    price_paise: int,
    order_remaining: int,
) -> None:
    """The buyer's side of a trade: reserved cash becomes shares."""
    r = _active(reservation, order_id)
    cost = quantity * price_paise
    if cost > r.remaining_cash_paise:
        raise SettlementError(
            f"order {order_id}: trade costs {cost}, {r.remaining_cash_paise} reserved"
        )
    r.remaining_cash_paise -= cost
    account.reserved_cash_paise -= cost
    account.cash_paise -= cost
    holding.avg_cost_paise = round_half_up(
        holding.quantity * holding.avg_cost_paise + cost, holding.quantity + quantity
    )
    holding.quantity += quantity
    if order_remaining == 0:  # finished: hand back any price improvement
        release(r, account, holding)
        r.status = ReservationStatus.CONSUMED


def settle_sell(
    reservation: Reservation | None,
    account: Account,
    holding: Holding,
    *,
    order_id: str,
    quantity: int,
    price_paise: int,
    order_remaining: int,
) -> None:
    """The seller's side of a trade: reserved shares become cash."""
    r = _active(reservation, order_id)
    if quantity > r.remaining_quantity:
        raise SettlementError(
            f"order {order_id}: sells {quantity}, {r.remaining_quantity} reserved"
        )
    r.remaining_quantity -= quantity
    holding.reserved_quantity -= quantity
    holding.quantity -= quantity
    if holding.quantity == 0:
        holding.avg_cost_paise = 0
    account.cash_paise += quantity * price_paise
    if order_remaining == 0:
        release(r, account, holding)
        r.status = ReservationStatus.CONSUMED


def portfolio_view(
    account: Account, holdings: list[Holding], last_prices: dict[str, int], as_of: str
) -> dict[str, Any]:
    """Q-09: cash, holdings at last traded prices, net worth and profit or loss."""
    rows = []
    market_value = 0
    for h in sorted(holdings, key=lambda h: h.ticker):
        if h.quantity == 0 and h.reserved_quantity == 0:
            continue
        price = last_prices[h.ticker]
        value = h.quantity * price
        market_value += value
        rows.append(
            {
                "ticker": h.ticker,
                "quantity": h.quantity,
                "reserved_quantity": h.reserved_quantity,
                "available_quantity": h.available_quantity,
                "avg_cost_paise": h.avg_cost_paise,
                "last_price_paise": price,
                "market_value_paise": value,
                "unrealised_pnl_paise": value - h.quantity * h.avg_cost_paise,
            }
        )
    net_worth = account.cash_paise + market_value
    pnl = net_worth - account.starting_value_paise
    return {
        "trader_id": account.trader_id,
        "nickname": account.nickname,
        "kind": account.kind.value,
        "cash_paise": account.cash_paise,
        "reserved_cash_paise": account.reserved_cash_paise,
        "available_cash_paise": account.available_cash_paise,
        "holdings": rows,
        "net_worth_paise": net_worth,
        "starting_value_paise": account.starting_value_paise,
        "pnl_paise": pnl,
        "pnl_percent": round(pnl * 100 / account.starting_value_paise, 2),
        "as_of": as_of,
    }
