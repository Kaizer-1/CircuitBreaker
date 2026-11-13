"""Responses and WebSocket messages validated against api/market-data-service.yaml.

The REST answers of Q-02 to Q-05 must match their response schemas, and every Q-06 message must
match the schema api/README.md names for it (they reuse Candle, Trade and OrderBookDepth).
"""

from functools import cache
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from market_data_service.config import Settings
from market_data_service.main import create_app
from tests.unit.test_ws import Market

REPO_ROOT = Path(__file__).resolve().parents[4]
API = REPO_ROOT / "api"
SPEC_URI = "https://circuitbreaker.local/api/market-data-service.yaml"
COMMON_URI = "https://circuitbreaker.local/api/shared/common.yaml"


@cache
def registry() -> Registry[Any]:
    spec = yaml.safe_load((API / "market-data-service.yaml").read_text())
    common = yaml.safe_load((API / "shared" / "common.yaml").read_text())
    text = yaml.safe_dump(spec).replace("./shared/common.yaml#", f"{COMMON_URI}#")
    spec = yaml.safe_load(text)
    return Registry().with_resources(
        [
            (SPEC_URI, Resource.from_contents(spec, default_specification=_draft())),
            (COMMON_URI, Resource.from_contents(common, default_specification=_draft())),
        ]
    )


def _draft() -> Any:
    from referencing.jsonschema import DRAFT202012

    return DRAFT202012


def check(schema_name: str, value: Any) -> None:
    validator = Draft202012Validator(
        {"$ref": f"{SPEC_URI}#/components/schemas/{schema_name}"},
        registry=registry(),
        format_checker=FormatChecker(),
    )
    errors = [f"{list(e.absolute_path)}: {e.message}" for e in validator.iter_errors(value)]
    assert errors == [], errors


def check_array(schema_name: str, values: list[Any]) -> None:
    for value in values:
        check(schema_name, value)


def test_rest_answers_and_websocket_messages_match_the_spec(settings: Settings) -> None:
    app = create_app(settings, readiness_checks=[])
    m = Market()
    with TestClient(app) as client:
        assert client.portal is not None
        client.portal.call(m.projection.start)
        app.state.projection, app.state.hub = m.projection, m.hub
        app.state.store = m.projection._store
        with client.websocket_connect("/v1/ws/market/CALVERA") as ws:
            snapshot = ws.receive_json()
            for _ in range(30):
                client.portal.call(m.step)
            messages = [snapshot]
            seen: set[str] = set()
            while not {"trade", "book", "candle"} <= seen:
                message = ws.receive_json()
                messages.append(message)
                seen.add(message["type"])

        instruments = client.get("/v1/instruments").json()
        candles = client.get("/v1/market/CALVERA/candles", params={"interval": "5s"}).json()
        book = client.get("/v1/market/CALVERA/order-book", params={"depth": 5}).json()
        trades = client.get("/v1/market/CALVERA/trades", params={"limit": 20}).json()
        bad = client.get("/v1/market/NOPE/order-book")

    check("InstrumentList", instruments)
    assert [i["ticker"] for i in instruments["instruments"]] == [
        "CALVERA",
        "KORVIN",
        "ASTRYX",
        "HELIOR",
        "TAVIRA",
    ]
    check("CandleSeries", candles)
    assert candles["candles"], "candles were stored"
    check("OrderBookDepth", book)
    check("TradeList", trades)
    assert 0 < len(trades["trades"]) <= 20
    assert bad.status_code == 422 and bad.json()["code"] == "VALIDATION_FAILED"

    for message in messages:
        kind, data = message["type"], message.get("data")
        if kind == "snapshot":
            check("OrderBookDepth", data["order_book"])
            check_array("Candle", data["candles"])
            check_array("Trade", data["recent_trades"])
            assert isinstance(data["last_price_paise"], int)
        elif kind == "trade":
            check("Trade", data)
        elif kind == "book":
            check("OrderBookDepth", data)
        elif kind == "candle":
            assert data["interval"] in ("5s", "1m", "5m") and isinstance(data["closed"], bool)
            check("Candle", data["candle"])
        else:
            assert kind == "ping"
