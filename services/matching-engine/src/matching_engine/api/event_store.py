"""Internal read API over the event store and the live books (api/matching-engine.yaml).

Thin routers: parse, ask the engine or its store, shape the answer as BookState / EventPage.

A snapshot (Q-15) carries the book's memory of an hour of finished orders: up to about 30,000 after
a panic-sell test, 5 MB of JSON. FastAPI's generic encoder took over 100 ms on that, on the event
loop that also matches orders, so Q-15 builds and encodes its answer once, in a worker thread.
"""

import asyncio
import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response

from matching_engine.application.engine import MatchingEngine
from matching_engine.domain.model import BookSnapshot, PriceLevel
from matching_engine.runtime.events import format_timestamp
from matching_engine.runtime.problem import problem_response

Ticker = Literal["CALVERA", "KORVIN", "ASTRYX", "HELIOR", "TAVIRA"]

router = APIRouter()


def _engine(request: Request) -> MatchingEngine | None:
    engine: MatchingEngine | None = getattr(request.app.state, "engine", None)
    return engine if engine is not None and engine.ready else None


def _recovering(request: Request) -> JSONResponse:
    return problem_response(
        503,
        "DEPENDENCY_UNAVAILABLE",
        "Engine is starting",
        detail="The order books are still being rebuilt from the event store.",
        instance=request.url.path,
    )


def _levels_from_view(levels: list[PriceLevel]) -> list[dict[str, Any]]:
    return [
        {
            "price_paise": lv.price_paise,
            "orders": [
                {
                    "order_id": o.order_id,
                    "trader_id": o.trader_id,
                    "remaining_quantity": o.remaining_quantity,
                    "accepted_sequence": o.accepted_sequence,
                }
                for o in lv.orders
            ],
        }
        for lv in levels
    ]


def _levels_from_snapshot(levels: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "price_paise": price,
            "orders": [
                {
                    "order_id": order_id,
                    "trader_id": trader_id,
                    "remaining_quantity": remaining,
                    "accepted_sequence": accepted,
                }
                for order_id, trader_id, remaining, accepted in orders
            ],
        }
        for price, orders in levels
    ]


@router.get(
    "/internal/v1/events",
    operation_id="listMarketEvents",
    summary="Read market events by sequence",
    openapi_extra={"x-seeds-id": "Q-14"},
)
async def list_market_events(
    request: Request,
    ticker: Ticker,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> Any:
    engine = _engine(request)
    if engine is None:
        return _recovering(request)
    cut = format_timestamp(until) if until is not None else None
    found = await engine.store.events_after(ticker, after_sequence, limit=limit + 1, until=cut)
    page = found[:limit]
    return {
        "ticker": ticker,
        "events": [e.model_dump(mode="json") for e in page],
        "next_after_sequence": page[-1].data["sequence"] if page else None,
        "has_more": len(found) > limit,
    }


@router.get(
    "/internal/v1/snapshots/{ticker}",
    operation_id="getBookSnapshot",
    summary="Get the nearest order-book snapshot at or before a time",
    openapi_extra={"x-seeds-id": "Q-15"},
)
async def get_book_snapshot(request: Request, ticker: Ticker, at: datetime) -> Any:
    engine = _engine(request)
    if engine is None:
        return _recovering(request)
    stored = await engine.store.latest_snapshot(ticker, format_timestamp(at))
    if stored is None:  # none yet: replay starts from the first event
        return {
            "ticker": ticker,
            "sequence": 0,
            "as_of": format_timestamp(at),
            "bids": [],
            "asks": [],
            "closed": [],
            "forget_before": None,
        }
    body = await asyncio.to_thread(_snapshot_body, ticker, stored)
    return Response(body, media_type="application/json")


def _snapshot_body(ticker: str, stored: dict[str, Any]) -> bytes:
    snapshot = BookSnapshot.from_dict(stored)  # also reads snapshots stored before closed_at
    content = {
        "ticker": ticker,
        "sequence": snapshot.sequence,
        "as_of": snapshot.as_of,
        "bids": _levels_from_snapshot(stored["bids"]),
        "asks": _levels_from_snapshot(stored["asks"]),
        # Finished and expired orders the book still remembers: Q-16's state_hash covers them,
        # so a rebuild elsewhere (time travel) needs them to reproduce the same hash.
        "closed": [
            {"order_id": order_id, "state": state, "trader_id": trader_id, "closed_at": closed_at}
            for order_id, state, trader_id, closed_at in snapshot.closed
        ],
        "forget_before": snapshot.forget_before,
    }
    # The same bytes JSONResponse would send, without its generic per-value encoder.
    return json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode()


@router.get(
    "/internal/v1/books/{ticker}",
    operation_id="getLiveBook",
    summary="Get the engine's live in-memory order book",
    openapi_extra={"x-seeds-id": "Q-16"},
)
async def get_live_book(request: Request, ticker: Ticker) -> Any:
    engine = _engine(request)
    if engine is None:
        return problem_response(
            404,
            "NOT_FOUND",
            "Book not loaded yet",
            detail="The engine is still rebuilding its books from the event store.",
            instance=request.url.path,
        )
    book = engine.book(ticker)
    return {
        "ticker": ticker,
        "sequence": book.sequence,
        "as_of": book.as_of or format_timestamp(datetime.now(UTC)),
        "bids": _levels_from_view(book.bids()),
        "asks": _levels_from_view(book.asks()),
        "forget_before": book.forget_before,
        "state_hash": book.state_hash(),
    }
