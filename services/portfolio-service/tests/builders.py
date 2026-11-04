"""Builders for matching.v1 events in the catalog's shape, and for reservation requests."""

from itertools import count

from portfolio_service.domain.model import OrderType, ReservationRequest, Side
from portfolio_service.runtime.events import EventEnvelope, build_event

T0 = "2026-11-20T10:42:01.262Z"
_seq = count(1)
_ids = count(1)


def oid(n: int) -> str:
    return f"0dde0000-0000-4000-8000-{n:012d}"


def _event(event_type: str, data: dict[str, object]) -> EventEnvelope:
    return build_event(
        event_type,
        {"sequence": next(_seq), "ticker": data.pop("ticker", "CHAIWALA"), **data},
        producer="matching-engine",
        environment="dev",
        correlation_id="test",
        event_id=f"e0e0e0e0-0000-4000-8000-{next(_ids):012d}",
    )


def buy(
    n: int, trader: str, qty: int, price: int | None, *, ticker: str = "CHAIWALA"
) -> ReservationRequest:
    kind = OrderType.LIMIT if price is not None else OrderType.MARKET
    return ReservationRequest(oid(n), trader, ticker, Side.BUY, kind, qty, price)


def sell(
    n: int, trader: str, qty: int, price: int | None, *, ticker: str = "CHAIWALA"
) -> ReservationRequest:
    kind = OrderType.LIMIT if price is not None else OrderType.MARKET
    return ReservationRequest(oid(n), trader, ticker, Side.SELL, kind, qty, price)


def accepted(n: int, trader: str, *, ticker: str = "CHAIWALA") -> EventEnvelope:
    return _event(
        "OrderAccepted",
        {
            "ticker": ticker,
            "order_id": oid(n),
            "trader_id": trader,
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 1,
            "limit_price_paise": 5,
            "protection_price_paise": None,
            "accepted_at": T0,
        },
    )


def trade(
    buy_n: int,
    buyer: str,
    sell_n: int,
    seller: str,
    qty: int,
    price: int,
    buy_left: int,
    sell_left: int,
    *,
    ticker: str = "CHAIWALA",
) -> EventEnvelope:
    return _event(
        "TradeExecuted",
        {
            "ticker": ticker,
            "trade_id": f"7a7a7a7a-0000-4000-8000-{next(_ids):012d}",
            "price_paise": price,
            "quantity": qty,
            "buy_order_id": oid(buy_n),
            "sell_order_id": oid(sell_n),
            "buyer_trader_id": buyer,
            "seller_trader_id": seller,
            "aggressor_side": "BUY",
            "buy_order_remaining_quantity": buy_left,
            "sell_order_remaining_quantity": sell_left,
            "executed_at": T0,
        },
    )


def cancelled(n: int, trader: str, qty: int, reason: str = "USER_REQUESTED") -> EventEnvelope:
    return _event(
        "OrderCancelled",
        {
            "order_id": oid(n),
            "trader_id": trader,
            "side": "BUY",
            "cancelled_quantity": qty,
            "reason": reason,
            "cancelled_at": T0,
        },
    )


def expired(n: int, trader: str) -> EventEnvelope:
    return _event("OrderExpired", {"order_id": oid(n), "trader_id": trader, "expired_at": T0})
