"""Event contracts: what order-service publishes matches contracts/events/, and it reads matching.v1
exactly as the event catalog shows it."""

import json
import re
from datetime import timedelta
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from order_service.domain.order import OrderStatus, OrderType, Side
from order_service.runtime.events import EventEnvelope
from tests.unit.application.test_orders import RAJU, World

REPO_ROOT = Path(__file__).resolve().parents[4]
CONTRACTS = REPO_ROOT / "contracts" / "events"
CATALOG = REPO_ROOT / "docs" / "03-architecture" / "03-event-catalog.md"


@cache
def validators() -> dict[str, Draft202012Validator]:
    schemas = [json.loads(p.read_text()) for p in CONTRACTS.glob("*.schema.json")]
    registry: Registry[Any] = Registry().with_resources(
        (s["$id"], Resource.from_contents(s)) for s in schemas
    )
    return {
        s["title"]: Draft202012Validator(s, registry=registry, format_checker=FormatChecker())
        for s in schemas
        if "x-topic" in s
    }


def errors_of(event: EventEnvelope) -> list[str]:
    validator = validators()[event.event_type]
    return [e.message for e in validator.iter_errors(json.loads(event.to_json()))]


def example(event_type: str) -> EventEnvelope:
    blocks = re.findall(r"```json\n(.*?)\n```", CATALOG.read_text(), re.S)
    [found] = [b for b in map(json.loads, blocks) if b.get("event_type") == event_type]
    return EventEnvelope.model_validate(found | {"added_later": True})


async def test_every_event_order_service_publishes_matches_its_schema() -> None:
    w = World()
    limit = await w.place()
    await w.place(order_type=OrderType.MARKET, limit_price_paise=None, side=Side.SELL, quantity=5)
    await w.orders.cancel(RAJU, limit.order_id, correlation_id="c")
    w.clock.now += timedelta(seconds=130)
    await w.orders.request_expiry("6c7d8e9f-0a1b-4c2d-8e3f-4a5b6c7d8e9f", RAJU, "CALVERA")
    sent = [event for _, _, event in w.publisher.sent]
    assert [e.event_type for e in sent] == [
        "OrderPlaced",
        "OrderPlaced",
        "OrderCancelRequested",
        "OrderExpiryRequested",
    ]
    for event in sent:
        assert errors_of(event) == [], event.event_type
        assert validators()[event.event_type].schema["x-topic"] == "orders.v1"


async def test_the_catalog_examples_of_matching_v1_drive_the_status() -> None:
    w = World()
    me = "7f3c9e10-2b4d-4e6f-8a1b-3c5d7e9f0a12"  # the order ID in the catalog's examples
    order = await w.place()
    stored = w.store.orders.pop(order.order_id)
    stored.order_id = me
    w.store.orders[me] = stored
    assert await w.orders.apply_matching_event(example("OrderAccepted"))
    assert (await w.orders.get(RAJU, me)).status is OrderStatus.OPEN
    assert await w.orders.apply_matching_event(example("TradeExecuted"))
    assert (await w.orders.get(RAJU, me)).filled_quantity == 20
    assert await w.orders.apply_matching_event(example("OrderCancelled"))
    o = await w.orders.get(RAJU, me)
    assert (o.status, o.cancel_reason) == (OrderStatus.CANCELLED, "USER_REQUESTED")
    # Examples about other orders change nothing here, but are read without error.
    assert await w.orders.apply_matching_event(example("OrderCancelRejected"))
    assert await w.orders.apply_matching_event(example("OrderExpired"))
