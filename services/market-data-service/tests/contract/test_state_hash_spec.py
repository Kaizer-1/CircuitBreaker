"""The time-travel hash follows matching-engine's rule (api/matching-engine.yaml, BookState).

This service rebuilds past books with its own code (domain/replay_book.py) and must get exactly
the hash the engine's Q-16 gives, or "hashes match" would prove nothing. The engine's spec states
the hash of its own Q-15 example; matching-engine has the twin of this test.
"""

import re
from pathlib import Path
from typing import Any

import yaml

from market_data_service.domain.replay_book import ReplayBook

SPEC = Path(__file__).resolve().parents[4] / "api" / "matching-engine.yaml"


def test_a_rebuilt_book_hashes_the_engine_specs_example_to_the_stated_hash() -> None:
    spec: dict[str, Any] = yaml.safe_load(SPEC.read_text())
    description = spec["components"]["schemas"]["BookState"]["properties"]["state_hash"][
        "description"
    ]
    stated = re.search(r"Q-15 example above, as a book, hashes to\s+`([0-9a-f]{64})`", description)
    assert stated is not None
    q15 = spec["paths"]["/internal/v1/snapshots/{ticker}"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["example"]
    book = ReplayBook.from_snapshot(
        q15["ticker"],
        sequence=q15["sequence"],
        as_of=q15["as_of"],
        bids=q15["bids"],
        asks=q15["asks"],
        closed=q15["closed"],
        forget_before=q15["forget_before"],
    )
    assert book.state_hash() == stated.group(1)


def test_the_engine_specs_q16_example_hash_is_reproduced() -> None:
    spec: dict[str, Any] = yaml.safe_load(SPEC.read_text())
    q16 = spec["paths"]["/internal/v1/books/{ticker}"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["example"]
    filled = {  # the order the Q-14 example's trade filled (see the comment in the spec)
        "order_id": "9e8d7c6b-5a4f-4e3d-8c2b-1a0f9e8d7c6b",
        "state": "FILLED",
        "trader_id": "b0b0b0b0-0003-4000-8000-000000000003",
        "closed_at": "2026-11-20T10:42:01.263Z",
    }
    book = ReplayBook.from_snapshot(
        q16["ticker"],
        sequence=q16["sequence"],
        as_of=q16["as_of"],
        bids=q16["bids"],
        asks=q16["asks"],
        closed=[filled],
        forget_before=q16["forget_before"],
    )
    assert book.state_hash() == q16["state_hash"]
