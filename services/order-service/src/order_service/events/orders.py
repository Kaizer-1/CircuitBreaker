"""orders.v1 events (docs/03-architecture/03-event-catalog.md): OrderPlaced, OrderCancelRequested,
OrderExpiryRequested. tests/contract/test_events.py validates them against contracts/events/."""

from datetime import datetime

from order_service.domain.order import Order
from order_service.runtime.events import (
    EventEnvelope,
    build_event,
    deterministic_event_id,
    format_timestamp,
)

PRODUCER = "order-service"


def order_placed(
    order: Order, at: datetime, *, environment: str, correlation_id: str
) -> EventEnvelope:
    return build_event(
        "OrderPlaced",
        {
            "order_id": order.order_id,
            "trader_id": order.trader_id,
            "ticker": order.ticker,
            "side": order.side.value,
            "order_type": order.order_type.value,
            "quantity": order.quantity,
            "limit_price_paise": order.limit_price_paise,
            "protection_price_paise": order.protection_price_paise,
            "reservation_id": order.reservation_id,
            "placed_at": order.placed_at,
        },
        producer=PRODUCER,
        environment=environment,
        correlation_id=correlation_id,
        occurred_at=at,
    )


def order_cancel_requested(
    order: Order, at: datetime, *, environment: str, correlation_id: str
) -> EventEnvelope:
    return build_event(
        "OrderCancelRequested",
        {
            "order_id": order.order_id,
            "trader_id": order.trader_id,
            "ticker": order.ticker,
            "requested_at": format_timestamp(at),
        },
        producer=PRODUCER,
        environment=environment,
        correlation_id=correlation_id,
        occurred_at=at,
    )


def order_expiry_requested(
    order_id: str, trader_id: str, ticker: str, at: datetime, *, environment: str
) -> EventEnvelope:
    """The event_id depends only on the order (UUIDv5), so asking twice is one command."""
    return build_event(
        "OrderExpiryRequested",
        {
            "order_id": order_id,
            "trader_id": trader_id,
            "ticker": ticker,
            "requested_at": format_timestamp(at),
        },
        producer=PRODUCER,
        environment=environment,
        correlation_id=f"expiry-{order_id}",
        event_id=deterministic_event_id(f"https://circuitbreaker.local/order-expiry/{order_id}"),
        occurred_at=at,
    )
