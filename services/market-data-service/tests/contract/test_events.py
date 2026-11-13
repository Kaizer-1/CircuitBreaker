"""Event contracts: CandleClosed matches contracts/events/, and market-data reads matching.v1
exactly as the event catalog shows it (the catalog's own examples, with an unknown field added)."""

import json
import re
from datetime import timedelta
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from market_data_service.runtime.events import EventEnvelope
from tests.unit.application.test_projection import World

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


async def test_every_candle_closed_market_data_publishes_matches_its_schema() -> None:
    w = World()
    await w.projection.start()
    w.trade(24600, 10)
    await w.deliver_all()
    w.clock.now += timedelta(minutes=6)  # closes 5s, 1m and 5m candles, flat ones included
    await w.projection.tick()
    sent = [event for _, _, event in w.publisher.sent]
    assert {e.data["interval"] for e in sent} == {"5s", "1m", "5m"}
    for event in sent:
        assert errors_of(event) == [], event.data


async def test_the_catalog_examples_of_matching_v1_are_applied() -> None:
    w = World()
    await w.projection.start()
    accepted, traded = example("OrderAccepted"), example("TradeExecuted")
    w.events.events["CALVERA"] = []  # the catalog example's sequences start at 1041
    w.projection.markets["CALVERA"].sequence = 1040
    for event in (accepted, traded):
        await w.projection.handle(event)
    market = w.projection.markets["CALVERA"]
    assert market.sequence == 1042 and market.last_price_paise == 24550
    bids, _ = market.depth_levels(10)
    assert [(b.price_paise, b.quantity) for b in bids] == [(24600, 30)]  # 50 accepted, 20 traded


async def test_events_market_data_does_not_show_still_advance_the_sequence() -> None:
    w = World()
    await w.projection.start()
    w.events.events["CALVERA"] = []
    for name, sequence in (("OrderCancelRejected", 1103), ("OrderExpired", 1188)):
        w.projection.markets["CALVERA"].sequence = sequence - 1
        await w.projection.handle(example(name))
        assert w.projection.markets["CALVERA"].sequence == sequence
    assert w.publisher.raw == []  # nothing dead-lettered
