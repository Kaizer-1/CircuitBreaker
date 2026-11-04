"""The envelope that wraps every integration event (docs/03-architecture/03-event-catalog.md).

Only the envelope is shared; each service defines the `data` of the events it produces or consumes
in its own `events/` package. Parsing is tolerant (unknown fields are ignored); building is exact.
"""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, ValidationError


class InvalidEventError(ValueError):
    """Not a valid event envelope (bad JSON, a missing or wrong field). Never worth retrying."""


def format_timestamp(moment: datetime) -> str:
    """ISO-8601 UTC with millisecond precision, e.g. 2026-11-20T10:42:01.250Z."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def deterministic_event_id(url: str) -> str:
    """UUIDv5 (URL namespace): the same request always gets the same event_id, so it dedupes."""
    return str(uuid5(NAMESPACE_URL, url))


class EventEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    event_id: str
    event_type: str
    event_version: int
    occurred_at: str
    producer: str
    environment: str | None = None
    correlation_id: str
    causation_id: str | None = None
    data: dict[str, Any]

    def to_json(self) -> bytes:
        return json.dumps(self.model_dump(mode="json"), separators=(",", ":")).encode()

    @classmethod
    def from_json(cls, raw: bytes) -> "EventEnvelope":
        try:
            return cls.model_validate_json(raw)
        except ValidationError as exc:
            raise InvalidEventError(str(exc)) from exc


def build_event(
    event_type: str,
    data: dict[str, Any],
    *,
    producer: str,
    environment: str,
    correlation_id: str,
    causation_id: str | None = None,
    event_id: str | None = None,
    occurred_at: datetime | None = None,
    event_version: int = 1,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id or str(uuid4()),
        event_type=event_type,
        event_version=event_version,
        occurred_at=format_timestamp(occurred_at or datetime.now(UTC)),
        producer=producer,
        environment=environment,
        correlation_id=correlation_id,
        causation_id=causation_id,
        data=data,
    )
