"""Q-11, the leaderboard: traders ranked by net worth, recomputed at most every 2 seconds.

Net worth is marked to market exactly as the portfolio view (Q-09) does it: cash plus every share
at the last traded price (the reference price before a ticker's first trade). By default only
humans are ranked; `include_bots` adds the 20 bots; load-test accounts (LOAD) are never ranked,
they are not people playing.

**Why a 2-second cache.** The presenter dashboard polls every 2 seconds, and so might every phone.
Ranking reads every account and holding, so each replica computes it at most once per interval
and per audience (with or without bots) and serves that answer meanwhile ("updated every few
seconds", docs/03-architecture/04-event-sourcing-and-cqrs.md, section 6). A lock makes callers
that arrive together wait for one computation instead of each starting their own.
"""

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from portfolio_service.domain.model import REFERENCE_PRICES, AccountKind
from portfolio_service.domain.rules import leaderboard
from portfolio_service.ports.store import PortfolioStore
from portfolio_service.runtime.events import format_timestamp

REFRESH_SECONDS = 2.0
HUMANS = (AccountKind.HUMAN,)
HUMANS_AND_BOTS = (AccountKind.HUMAN, AccountKind.BOT)
MAX_ENTRIES = 100  # the largest `limit` the spec allows


class Leaderboard:
    def __init__(
        self,
        store: PortfolioStore,
        *,
        refresh_seconds: float = REFRESH_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._refresh = refresh_seconds
        self._monotonic = monotonic
        self._clock = clock
        self._cached: dict[bool, tuple[float, dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def get(self, *, include_bots: bool, limit: int) -> dict[str, Any]:
        async with self._lock:
            cached = self._cached.get(include_bots)
            if cached is None or self._monotonic() - cached[0] >= self._refresh:
                cached = (self._monotonic(), await self._compute(include_bots))
                self._cached[include_bots] = cached
        board = cached[1]
        return {"as_of": board["as_of"], "entries": board["entries"][:limit]}

    async def _compute(self, include_bots: bool) -> dict[str, Any]:
        async with self._store.transaction() as tx:
            standings = await tx.standings(HUMANS_AND_BOTS if include_bots else HUMANS)
            prices = REFERENCE_PRICES | await tx.last_prices()
        return {
            "as_of": format_timestamp(self._clock()),
            "entries": leaderboard(standings, prices, MAX_ENTRIES),
        }
