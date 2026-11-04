"""orders.v1 messages -> engine commands. Unknown event types are ignored (tolerant reader)."""

from matching_engine.domain.model import (
    CancelOrder,
    Command,
    ExpireOrder,
    InvalidCommandError,
    OrderType,
    PlaceOrder,
    Side,
)
from matching_engine.runtime.events import EventEnvelope


def parse_command(envelope: EventEnvelope) -> Command | None:
    d = envelope.data
    common = {"command_id": envelope.event_id, "correlation_id": envelope.correlation_id}
    try:
        if envelope.event_type == "OrderPlaced":
            return PlaceOrder(
                **common,
                ticker=d["ticker"],
                order_id=d["order_id"],
                trader_id=d["trader_id"],
                side=Side(d["side"]),
                order_type=OrderType(d["order_type"]),
                quantity=int(d["quantity"]),
                limit_price_paise=d.get("limit_price_paise"),
                protection_price_paise=d.get("protection_price_paise"),
                placed_at=d["placed_at"],
            )
        if envelope.event_type == "OrderCancelRequested":
            return CancelOrder(
                **common,
                ticker=d["ticker"],
                order_id=d["order_id"],
                trader_id=d["trader_id"],
                requested_at=d["requested_at"],
            )
        if envelope.event_type == "OrderExpiryRequested":
            return ExpireOrder(
                **common,
                ticker=d["ticker"],
                order_id=d["order_id"],
                trader_id=d["trader_id"],
                requested_at=d["requested_at"],
            )
    except (KeyError, ValueError, TypeError) as exc:
        raise InvalidCommandError(f"malformed {envelope.event_type}: {exc!r}") from exc
    return None
