"""Event contract tests: what the engine publishes must match contracts/events/*.schema.json, and it
must understand the commands exactly as the event catalog shows them."""

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

from hypothesis import given
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from matching_engine.domain.model import CancelOrder, ExpireOrder, PlaceOrder
from matching_engine.events.commands import parse_command
from matching_engine.events.market import from_envelope, to_envelope
from matching_engine.runtime.events import EventEnvelope
from tests.property.harness import run
from tests.property.workload import Action, actions

REPO_ROOT = Path(__file__).resolve().parents[4]
CONTRACTS = REPO_ROOT / "contracts" / "events"
CATALOG = REPO_ROOT / "docs" / "03-architecture" / "03-event-catalog.md"
PRODUCED = {
    "OrderAccepted",
    "TradeExecuted",
    "OrderCancelled",
    "OrderCancelRejected",
    "OrderExpired",
}


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


def catalog_examples(event_type: str) -> list[dict[str, Any]]:
    blocks = re.findall(r"```json\n(.*?)\n```", CATALOG.read_text(), re.S)
    return [b for b in map(json.loads, blocks) if b.get("event_type") == event_type]


def assert_valid(envelope: EventEnvelope) -> None:
    validator = validators()[envelope.event_type]
    errors = [
        f"{list(e.absolute_path)}: {e.message}"
        for e in validator.iter_errors(json.loads(envelope.to_json()))
    ]
    assert errors == [], f"{envelope.event_type} breaks its contract: {errors}"


def test_every_event_type_the_engine_produces_has_a_schema_on_matching_v1() -> None:
    for event_type in PRODUCED:
        schema = validators()[event_type].schema
        assert (schema["x-topic"], schema["x-producer"]) == ("matching.v1", "matching-engine")


@given(actions)
def test_every_event_the_engine_produces_validates_against_its_schema(
    workload: list[Action],
) -> None:
    for event in run(workload).events:
        assert event.event_type in PRODUCED
        assert_valid(to_envelope(event, "dev"))


def test_the_catalog_examples_of_orders_v1_are_understood() -> None:
    [placed] = catalog_examples("OrderPlaced")
    command = parse_command(EventEnvelope.model_validate(placed))
    assert isinstance(command, PlaceOrder)
    assert (command.command_id, command.quantity, command.limit_price_paise) == (
        placed["event_id"],
        50,
        24600,
    )
    [cancel] = catalog_examples("OrderCancelRequested")
    assert isinstance(parse_command(EventEnvelope.model_validate(cancel)), CancelOrder)
    [expiry] = catalog_examples("OrderExpiryRequested")
    parsed = parse_command(EventEnvelope.model_validate(expiry))
    assert isinstance(parsed, ExpireOrder)
    assert parsed.order_id == expiry["data"]["order_id"]


def test_an_unknown_event_type_on_orders_v1_is_ignored() -> None:
    [placed] = catalog_examples("OrderPlaced")
    other = dict(placed, event_type="OrderAmended")
    assert parse_command(EventEnvelope.model_validate(other)) is None


def test_the_catalog_examples_of_matching_v1_validate_and_parse_back() -> None:
    for event_type in PRODUCED:
        [example] = catalog_examples(event_type)
        envelope = EventEnvelope.model_validate(example)
        assert_valid(envelope)
        assert from_envelope(envelope).event_type == event_type
