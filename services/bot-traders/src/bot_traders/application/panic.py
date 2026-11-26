"""A-23/A-24: the traffic spike as a panic sell (chaos scenarios section 3.3, ADR-0020).

chaos-service asks bot-traders to panic; the bots' own accounts then dump shares at market. The
credentials never leave this service. The mix is the one the rate was calibrated with
(`scripts/load_orders.py --mix panic`): request n goes out at start + n / rate, from bot-01 to
bot-20 in turn, 9 MARKET sells of 1 share (on a random ticker) for every `GET /v1/orders`.

It is open loop, like a crowd: a request goes out on schedule whether or not the earlier ones were
answered, and none is retried, so the offered rate is the rate asked for even while order-service
is overloaded (a closed loop would slow down with it and hide the overload). At most MAX_IN_FLIGHT
wait for an answer at once; beyond that a request is counted as `dropped` instead of sent, to keep
this process's own memory bounded. It ends by itself after `duration_seconds`.

A sell finds a bid or is cancelled by its protection price (last price -10%), which releases its
shares, so the burst never runs out; MARKET orders never rest, so nothing is left to clean up. The
normal strategies keep trading meanwhile: the market makers' bids are what the panic sells into,
which is why the price falls on the dashboard.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from random import Random

from bot_traders.domain.market import TICKERS
from bot_traders.ports.exchange import PanicExchange, Session

logger = logging.getLogger("panic")

BOT_ACCOUNTS = tuple(f"bot-{n:02d}" for n in range(1, 21))
MIX = ("SELL",) * 9 + ("GET",)
MAX_IN_FLIGHT = 2_000


@dataclass(frozen=True)
class PanicState:
    requests_per_second: int
    duration_seconds: int
    started_at: datetime

    @property
    def ends_at(self) -> datetime:
        return self.started_at + timedelta(seconds=self.duration_seconds)


class PanicAlreadyRunningError(Exception):
    def __init__(self, state: PanicState) -> None:
        super().__init__("PANIC_SELL")
        self.state = state


class PanicSell:
    def __init__(
        self,
        exchange: PanicExchange,
        *,
        rng: Random,
        accounts: Sequence[str] = BOT_ACCOUNTS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_sent: Callable[[str, str], None] = lambda kind, result: None,
    ) -> None:
        self._exchange = exchange
        self._rng = rng
        self._accounts = accounts
        self._clock = clock
        self._sleep = sleep
        self._on_sent = on_sent
        self._sessions: list[Session] = []
        self._task: asyncio.Task[None] | None = None
        self._requests: set[asyncio.Task[None]] = set()
        self.state: PanicState | None = None

    async def start(
        self, requests_per_second: int, duration_seconds: int, now: datetime
    ) -> PanicState:
        if self.state is not None:
            raise PanicAlreadyRunningError(self.state)
        # Its own sessions, made afresh for each burst: they work whether or not the bots are
        # trading, and a token from an earlier burst may have expired (tokens last 12 hours; on
        # staging, 2026-11-26, a panic a day after the first sent only 401s and no load at all).
        self._sessions = [await self._exchange.login(a) for a in self._accounts]
        self.state = PanicState(requests_per_second, duration_seconds, now)
        self._task = asyncio.create_task(self._run(self.state))
        details = {"fault": "PANIC_SELL", "requests_per_second": requests_per_second}
        logger.info("FaultInjected", extra=details | {"duration_seconds": duration_seconds})
        return self.state

    async def stop(self) -> bool:
        """End the burst now; requests already sent finish on their own. False if none ran."""
        if self.state is None or self._task is None:
            return False
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        return True

    async def wait(self) -> None:
        """For tests: until the burst has ended and every request was answered."""
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        # Not `while self._requests`: gathering tasks that are already done returns without
        # yielding (Python 3.12), so their done-callbacks would never get to leave the set.
        while pending := [task for task in self._requests if not task.done()]:
            await asyncio.gather(*pending)

    async def _run(self, state: PanicState) -> None:
        rate, sent = state.requests_per_second, 0
        began = self._clock()
        try:
            for slot in range(rate * state.duration_seconds):
                delay = began + slot / rate - self._clock()
                if delay > 0:
                    await self._sleep(delay)
                kind = MIX[(slot // len(self._sessions)) % len(MIX)]
                if len(self._requests) >= MAX_IN_FLIGHT:
                    self._on_sent(kind, "dropped")
                    continue
                session = self._sessions[slot % len(self._sessions)]
                task = asyncio.create_task(self._send(kind, session))
                self._requests.add(task)
                task.add_done_callback(self._requests.discard)
                sent += 1
        finally:
            self.state, self._task = None, None
            logger.info("FaultCleared", extra={"fault": "PANIC_SELL", "requests_sent": sent})

    async def _send(self, kind: str, session: Session) -> None:
        if kind == "SELL":
            status = await self._exchange.market_sell(session, self._rng.choice(TICKERS))
        else:
            status = await self._exchange.list_orders(session)
        self._on_sent(kind, str(status) if status else "no_answer")
