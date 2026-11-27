"""The live incident feed (Q-19, `WS /v1/ws/incidents`): one queue per connected screen.

Messages are those of api/README.md: `snapshot` on connect, then `incident_opened`,
`incident_update` (one timeline entry), `approval_required` and `incident_resolved`, and every few
seconds `vitals` (application/vitals.py). A screen that stops reading is dropped rather than slowing
the agent down. Nothing in the agent reads from the hub: it is an output only.
"""

import asyncio
import logging
from typing import Any

logger = logging.getLogger("feed")
QUEUE = 200


class FeedHub:
    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[dict[str, Any]]] = set()

    @property
    def screens(self) -> int:
        return len(self._queues)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(QUEUE)
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._queues.discard(queue)

    def publish(self, kind: str, data: dict[str, Any]) -> None:
        message = {"type": kind, "data": data}
        for queue in list(self._queues):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning("a feed screen fell behind and was dropped")
                self._queues.discard(queue)
