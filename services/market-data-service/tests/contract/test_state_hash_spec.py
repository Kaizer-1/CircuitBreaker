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
    )
    assert book.state_hash() == stated.group(1)
