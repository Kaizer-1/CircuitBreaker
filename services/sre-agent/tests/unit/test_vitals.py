"""The control room's vitals (owner, 2026-11-27): pushed on the feed for screens, never read back.

The publisher reads the same numbers the evidence collector would, sends nothing when Prometheus
does not answer (the screen then greys out), and a screen that connects gets the last vitals only
while they are fresh. The agent's own evidence is the same whether vitals are published or not."""

from typing import Any

from fastapi.testclient import TestClient

from sre_agent.application.feed import FeedHub
from sre_agent.application.vitals import STALE_SECONDS, VitalsPublisher
from sre_agent.domain.actions import Proposal, Tool
from sre_agent.main import create_app
from tests.fakes import Clock, FakeLLM, FakeMetrics, World
from tests.unit.test_agent import Rig, bad_deploy, keep_ticking
from tests.unit.test_api import settings


def market(world: World) -> World:
    world.metrics["order-service"] = {
        "rate": 71.5,
        "error_ratio": 0.2072,
        "p95": 2.31,
        "replicas_desired": 5,
        "replicas_ready": 3,
    }
    world.metrics["market-data-service"] = {"memory_of_limit": 0.734}
    return world


def publisher(world: World, feed: FeedHub, metrics: FakeMetrics | None = None) -> VitalsPublisher:
    return VitalsPublisher(metrics or FakeMetrics(world), feed, "staging", world.clock.now)


async def test_vitals_are_the_numbers_the_agent_sees_sent_to_every_screen() -> None:
    world = market(World(Clock()))
    feed = FeedHub()
    screen = feed.subscribe()
    assert await publisher(world, feed).publish_once()
    message: dict[str, Any] = screen.get_nowait()
    assert message["type"] == "vitals"
    assert message["data"] == {
        "at": "2026-11-26T11:00:00.000Z",
        "service": "order-service",
        "requests_per_second": 71.5,
        "error_ratio": 0.2072,
        "p95_seconds": 2.31,
        "replicas": {"desired": 5, "ready": 3},
        "market_data_memory_of_limit": 0.734,
    }


async def test_without_prometheus_nothing_is_sent_and_a_new_screen_gets_no_old_numbers() -> None:
    world = market(World(Clock()))
    feed = FeedHub()
    metrics = FakeMetrics(world)
    vitals = publisher(world, feed, metrics)
    assert await vitals.publish_once()
    assert vitals.current() is not None

    async def down(query: str) -> float | None:
        raise ConnectionError("prometheus down")

    metrics.value = down  # type: ignore[method-assign]
    screen = feed.subscribe()
    await world.clock.sleep(STALE_SECONDS - 1)
    assert not await vitals.publish_once()
    assert screen.empty()  # nothing pretends to be current
    assert vitals.current() is not None  # 14 s old: still fresh
    await world.clock.sleep(2)
    assert vitals.current() is None  # 16 s old: stale, not handed to a new screen


def test_a_screen_that_connects_gets_fresh_vitals_right_after_the_snapshot() -> None:
    rig = Rig(market(World(Clock())))
    app = create_app(settings(), [], agent=rig.agent, store=rig.store)
    vitals = publisher(rig.world, rig.agent.feed)
    with TestClient(app) as c:
        app.state.vitals = vitals
        with c.websocket_connect("/v1/ws/incidents") as ws:  # none published yet
            assert ws.receive_json()["type"] == "snapshot"
        c.portal.call(vitals.publish_once)  # type: ignore[union-attr]
        with c.websocket_connect("/v1/ws/incidents") as ws:
            assert ws.receive_json()["type"] == "snapshot"
            assert ws.receive_json()["data"]["p95_seconds"] == 2.31


async def test_the_agent_never_reads_its_own_vitals_as_evidence() -> None:
    """The same bad deploy twice, once with vitals published on the agent's own feed before every
    poll: the model is shown exactly the same evidence, and none of it is a vitals message."""

    async def evidence(with_vitals: bool) -> list[Any]:
        world = World(Clock())
        bad_deploy(world)
        world.metrics["market-data-service"] = {"memory_of_limit": 0.2}
        rollback = Proposal(Tool.ROLLBACK.value, "order-service", None, "Bad release.", "llm:t")
        llm = FakeLLM(rollback)
        rig = Rig(world, llm)
        vitals = publisher(world, rig.agent.feed)
        if with_vitals:
            await vitals.publish_once()
        await rig.run()
        for _ in range(12):
            if with_vitals:
                await vitals.publish_once()
            await keep_ticking(rig, 5)
        return llm.seen

    plain, alongside = await evidence(False), await evidence(True)
    assert plain and plain == alongside
    assert "vitals" not in repr(alongside)
