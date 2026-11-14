"""Property (Q-07 status=open): the open-orders table always equals the orders not yet finished.

1,000 random workloads (open_orders_workload.py) on the in-memory store, which keeps the table by
the same rule as Cassandra's; tests/integration/test_open_orders_cassandra.py plays the same
workloads on a real Cassandra.
"""

import asyncio
from typing import Any

from hypothesis import given

from tests.fakes import InMemoryOrderStore
from tests.property.open_orders_workload import run, workloads


@given(workloads)
def test_the_open_orders_table_equals_the_orders_not_finished(
    workload: list[tuple[Any, ...]],
) -> None:
    asyncio.run(run(InMemoryOrderStore(), workload))
