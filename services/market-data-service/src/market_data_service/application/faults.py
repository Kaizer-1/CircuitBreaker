"""The memory-leak fault (A-12/A-13; chaos scenarios section 3.2): a cache that never evicts.

While it is on, every second adds `rate_mb_per_minute / 60` MiB to a list nobody reads, so memory
climbs in a straight line toward the pod's limit, as a real leak would. It grows on a timer, not
per WebSocket message, so the rate does not depend on how many charts are open.

Each piece is an anonymous memory map with every page written, so it is really resident (counted in
the container's working set, which the alert reads) and is handed straight back to the operating
system when the fault is cleared: a reset brings memory back down without a restart.

Nothing about the fault is exported as a metric: the SRE agent must work out the cause from the
symptoms (chaos scenarios section 1, "blind diagnosis"). Switching it on or off is logged
(`FaultInjected` / `FaultCleared`, local events in the event catalog).
"""

import asyncio
import contextlib
import logging
import mmap
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger("faults")

MIB = 1024 * 1024
PAGE = mmap.PAGESIZE


@dataclass(frozen=True)
class FaultState:
    fault: str
    rate_mb_per_minute: int
    started_at: datetime


class FaultAlreadyActiveError(Exception):
    def __init__(self, state: FaultState) -> None:
        super().__init__(state.fault)
        self.state = state


class MemoryLeak:
    def __init__(self, tick_seconds: float = 1.0) -> None:
        self.tick_seconds = tick_seconds
        self.state: FaultState | None = None
        self._pieces: list[mmap.mmap] = []
        self._owed = 0.0  # bytes due but smaller than a page
        self._task: asyncio.Task[None] | None = None

    @property
    def held_bytes(self) -> int:
        return sum(len(piece) for piece in self._pieces)

    def start(self, rate_mb_per_minute: int, now: datetime) -> FaultState:
        if self.state is not None:
            raise FaultAlreadyActiveError(self.state)
        self.state = FaultState("MEMORY_LEAK", rate_mb_per_minute, now)
        self._task = asyncio.create_task(self._run())
        details = {"fault": "MEMORY_LEAK", "rate_mb_per_minute": rate_mb_per_minute}
        logger.info("FaultInjected", extra=details)
        return self.state

    async def stop(self) -> bool:
        """Switch the fault off and give back what it held. False if it was not on."""
        if self.state is None:
            return False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        freed = self.held_bytes
        for piece in self._pieces:
            piece.close()
        self._pieces.clear()
        self._owed = 0.0
        self.state, self._task = None, None
        logger.info("FaultCleared", extra={"fault": "MEMORY_LEAK", "freed_mib": freed // MIB})
        return True

    def grow(self) -> None:
        """One tick's worth of leak (called every `tick_seconds` while the fault is on)."""
        if self.state is None:
            return
        self._owed += self.state.rate_mb_per_minute * MIB * self.tick_seconds / 60
        size = int(self._owed) // PAGE * PAGE
        if size == 0:
            return
        self._owed -= size
        piece = mmap.mmap(-1, size)
        for offset in range(0, size, PAGE):  # write every page, so it is really resident
            piece[offset] = 1
        self._pieces.append(piece)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.tick_seconds)
            self.grow()
