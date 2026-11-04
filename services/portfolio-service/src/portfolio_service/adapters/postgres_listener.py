"""LISTEN on `portfolio_changes`: every replica hears every committed change (Q-10).

A dedicated autocommit connection, outside the pool. If it drops, a NOTIFY sent meanwhile is
lost, so after reconnecting the hub re-reads every connected portfolio once.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from portfolio_service.adapters.postgres import CHANGES_CHANNEL

logger = logging.getLogger("listener")


class PostgresListener:
    def __init__(
        self,
        conninfo: str,
        on_change: Callable[[str], Awaitable[None]],
        on_reconnect: Callable[[], Awaitable[None]],
    ) -> None:
        self._conninfo = conninfo
        self._on_change = on_change
        self._on_reconnect = on_reconnect
        self.listening = False

    async def run(self) -> None:
        import psycopg

        first = True
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(
                    self._conninfo, autocommit=True
                ) as conn:
                    await conn.execute(f"LISTEN {CHANGES_CHANNEL}")
                    self.listening = True
                    if not first:
                        await self._on_reconnect()
                    first = False
                    async for notification in conn.notifies():
                        try:
                            await self._on_change(notification.payload)
                        except Exception:
                            logger.exception("pushing a portfolio change failed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("LISTEN connection lost, reconnecting", extra={"error": repr(exc)})
            self.listening = False
            await asyncio.sleep(1.0)
