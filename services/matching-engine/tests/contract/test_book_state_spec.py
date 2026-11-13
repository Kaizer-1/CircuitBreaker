"""BookState in api/matching-engine.yaml: real Q-15/Q-16 answers match it, and its hash rule holds.

`state_hash` is a cross-service contract: market-data-service rebuilds books for time travel (Q-12)
with its own code and must get the same hash. So the spec states the hash of its own Q-15 example,
and this test (and its twin in market-data-service) checks it.
"""

import re
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from matching_engine.application.engine import MatchingEngine
from matching_engine.domain.book import OrderBook
from matching_engine.domain.model import BookSnapshot
from tests.unit.test_api import client, engine  # noqa: F401 (pytest fixtures)

API = Path(__file__).resolve().parents[4] / "api"
SPEC_URI = "https://circuitbreaker.local/api/matching-engine.yaml"
COMMON_URI = "https://circuitbreaker.local/api/shared/common.yaml"


@cache
def spec() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((API / "matching-engine.yaml").read_text())
    return loaded


@cache
def registry() -> Registry[Any]:
    text = yaml.safe_dump(spec()).replace("./shared/common.yaml#", f"{COMMON_URI}#")
    common = yaml.safe_load((API / "shared" / "common.yaml").read_text())
    return Registry().with_resources(
        [
            (SPEC_URI, Resource.from_contents(yaml.safe_load(text), DRAFT202012)),
            (COMMON_URI, Resource.from_contents(common, DRAFT202012)),
        ]
    )


def check_book_state(value: Any) -> None:
    validator = Draft202012Validator(
        {"$ref": f"{SPEC_URI}#/components/schemas/BookState"},
        registry=registry(),
        format_checker=FormatChecker(),
    )
    errors = [f"{list(e.absolute_path)}: {e.message}" for e in validator.iter_errors(value)]
    assert errors == [], errors


def example(path: str) -> dict[str, Any]:
    answer: dict[str, Any] = spec()["paths"][path]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["example"]
    return answer


def as_snapshot(book: dict[str, Any], closed: list[dict[str, str]]) -> BookSnapshot:
    """A BookState answer (objects) in the canonical snapshot form (lists)."""

    def levels(side: list[dict[str, Any]]) -> list[Any]:
        return [
            [
                lv["price_paise"],
                [
                    [o["order_id"], o["trader_id"], o["remaining_quantity"], o["accepted_sequence"]]
                    for o in lv["orders"]
                ],
            ]
            for lv in side
        ]

    return BookSnapshot.from_dict(
        {
            "ticker": book["ticker"],
            "sequence": book["sequence"],
            "as_of": book["as_of"],
            "bids": levels(book["bids"]),
            "asks": levels(book["asks"]),
            "closed": [[c["order_id"], c["state"], c["trader_id"]] for c in closed],
        }
    )


def test_the_spec_states_the_true_hash_of_its_q15_example() -> None:
    description = spec()["components"]["schemas"]["BookState"]["properties"]["state_hash"][
        "description"
    ]
    stated = re.search(r"Q-15 example above, as a book, hashes to\s+`([0-9a-f]{64})`", description)
    assert stated is not None, "the state_hash description must give the Q-15 example's hash"
    q15 = example("/internal/v1/snapshots/{ticker}")
    book = OrderBook.from_snapshot(as_snapshot(q15, q15["closed"]))
    assert book.state_hash() == stated.group(1)


def test_the_q16_example_hash_is_real() -> None:
    q16 = example("/internal/v1/books/{ticker}")
    filled = [  # the order the Q-14 example's trade filled (see the comment in the spec)
        {
            "order_id": "9e8d7c6b-5a4f-4e3d-8c2b-1a0f9e8d7c6b",
            "state": "FILLED",
            "trader_id": "b0b0b0b0-0003-4000-8000-000000000003",
        }
    ]
    assert OrderBook.from_snapshot(as_snapshot(q16, filled)).state_hash() == q16["state_hash"]


def test_the_spec_examples_match_book_state() -> None:
    check_book_state(example("/internal/v1/snapshots/{ticker}"))
    check_book_state(example("/internal/v1/books/{ticker}"))


def test_q15_and_q16_answers_match_book_state_and_the_hash_rule(
    client: TestClient,  # noqa: F811
    engine: MatchingEngine,  # noqa: F811
) -> None:
    q15 = client.get("/internal/v1/snapshots/CALVERA", params={"at": "2026-11-20T11:00:00Z"})
    check_book_state(q15.json())
    empty = client.get("/internal/v1/snapshots/ASTRYX", params={"at": "2026-11-20T11:00:00Z"})
    check_book_state(empty.json())
    q16 = client.get("/internal/v1/books/CALVERA").json()
    check_book_state(q16)
    # The engine's own hash and the one computed from the API's answer by the spec's rule agree.
    live = engine.book("CALVERA").snapshot().to_dict()
    closed = [{"order_id": o, "state": s, "trader_id": t} for o, s, t in live["closed"]]
    assert OrderBook.from_snapshot(as_snapshot(q16, closed)).state_hash() == q16["state_hash"]
