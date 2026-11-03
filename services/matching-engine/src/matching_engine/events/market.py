"""matching.v1 events: domain events <-> catalog envelopes (docs/03-architecture/03-event-catalog).

The event store keeps the full envelope, so `from_envelope(to_envelope(e)) == e` must always hold:
replay rebuilds the book from these.
"""

from dataclasses import fields
from enum import StrEnum
from typing import Any

from matching_engine.domain.model import (
    MarketEvent,
    OrderAccepted,
    OrderCancelled,
    OrderCancelRejected,
    OrderExpired,
    TradeExecuted,
)
from matching_engine.runtime.events import EventEnvelope

PRODUCER = "matching-engine"
TOPIC = "matching.v1"

# Each event's own timestamp field; it always equals the envelope's occurred_at.
_TIME_FIELD: dict[type[MarketEvent], str] = {
    OrderAccepted: "accepted_at",
    TradeExecuted: "executed_at",
    OrderCancelled: "cancelled_at",
    OrderCancelRejected: "rejected_at",
    OrderExpired: "expired_at",
}
_BY_NAME = {cls.__name__: cls for cls in _TIME_FIELD}
_ENVELOPE_FIELDS = {"event_id", "occurred_at", "causation_id", "correlation_id"}


def to_envelope(event: MarketEvent, environment: str) -> EventEnvelope:
    data: dict[str, Any] = {
        f.name: getattr(event, f.name) for f in fields(event) if f.name not in _ENVELOPE_FIELDS
    }
    data = {k: (v.value if isinstance(v, StrEnum) else v) for k, v in data.items()}
    data[_TIME_FIELD[type(event)]] = event.occurred_at
    return EventEnvelope(
        event_id=event.event_id,
        event_type=event.event_type,
        event_version=1,
        occurred_at=event.occurred_at,
        producer=PRODUCER,
        environment=environment,
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
        data=data,
    )


def from_envelope(envelope: EventEnvelope) -> MarketEvent:
    cls = _BY_NAME.get(envelope.event_type)
    if cls is None:
        raise ValueError(f"not a matching.v1 event: {envelope.event_type}")
    meta = {
        "event_id": envelope.event_id,
        "occurred_at": envelope.occurred_at,
        "causation_id": envelope.causation_id,
        "correlation_id": envelope.correlation_id,
    }
    values: dict[str, Any] = {}
    for f in fields(cls):
        if f.name in meta:
            values[f.name] = meta[f.name]
            continue
        value = envelope.data[f.name]
        if isinstance(f.type, type) and issubclass(f.type, StrEnum):
            value = f.type(value)
        values[f.name] = value
    return cls(**values)
