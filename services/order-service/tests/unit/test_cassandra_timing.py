"""The Cassandra adapter times each driver call: the wait for a worker thread, then the call."""

import time

from order_service.adapters.cassandra_store import CassandraOrderStore


class SlowSession:
    def execute(self, query: object, params: object = ()) -> list[object]:
        time.sleep(0.02)
        return []


async def test_every_driver_call_reports_its_thread_wait_and_its_own_time() -> None:
    calls: list[tuple[float, float]] = []
    store = CassandraOrderStore(
        SlowSession(), on_call=lambda waited, ran: calls.append((waited, ran))
    )
    assert await store._run("SELECT 1") == []
    [(waited, ran)] = calls
    assert waited >= 0 and ran >= 0.02
