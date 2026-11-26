"""The Store port on Cassandra (keyspace sre_agent, role sre_agent_role).

An incident is one JSON document (its remediations and timeline inside), rewritten after every
change: the agent is the only writer (one replica, Recreate), and Q-18 is then one read. Listing
(Q-17) walks the per-day index newest first. The driver is synchronous, so every call runs in a
worker thread.
"""

import asyncio
import json
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from importlib import resources
from typing import Any, TypeVar

from sre_agent.domain.actions import APPLICATION_SERVICES
from sre_agent.domain.incident import Incident
from sre_agent.ports.store import ActionRecord, AgentSettings

T = TypeVar("T")
LOOK_BACK_DAYS = 30  # incidents.v1 keeps 30 days; Q-17 lists no further back
QUERIES = {
    "save": "INSERT INTO incidents (incident_id, opened_at, doc) VALUES (?, ?, ?)",
    "index": "INSERT INTO incidents_by_day (day, opened_at, incident_id) VALUES (?, ?, ?)",
    "get": "SELECT doc FROM incidents WHERE incident_id = ?",
    "day": "SELECT incident_id FROM incidents_by_day WHERE day = ? LIMIT ?",
    "day_before": "SELECT incident_id FROM incidents_by_day WHERE day = ? AND opened_at < ?"
    " LIMIT ?",
    "settings": "SELECT * FROM agent_settings WHERE environment = ?",
    "save_settings": "INSERT INTO agent_settings (environment, approval_required, paused,"
    " updated_at, updated_by) VALUES (?, ?, ?, ?, ?)",
    "record": "INSERT INTO action_log (service, executed_at, incident_id, action)"
    " VALUES (?, ?, ?, ?)",
    "actions": "SELECT * FROM action_log WHERE service = ? AND executed_at >= ?",
    "counter": "SELECT last FROM incident_counter WHERE name = 'incidents'",
    "counter_first": "INSERT INTO incident_counter (name, last) VALUES ('incidents', 1)"
    " IF NOT EXISTS",
    "counter_next": "UPDATE incident_counter SET last = ? WHERE name = 'incidents' IF last = ?",
}


def connect_cassandra(
    contact_points: Sequence[str], port: int, keyspace: str, username: str, password: str
) -> Any:
    """A session on the agent's own keyspace, logged in with its own role (as order-service)."""
    from cassandra.auth import PlainTextAuthProvider
    from cassandra.cluster import Cluster
    from cassandra.io.asyncioreactor import AsyncioConnection

    cluster = Cluster(
        list(contact_points),
        port=port,
        auth_provider=PlainTextAuthProvider(username, password),
        connection_class=AsyncioConnection,
        protocol_version=4,  # v5 framing failed its CRC with the asyncio reactor (Phase 3)
        connect_timeout=5,
    )
    return cluster.connect(keyspace)


def migration_statements() -> list[str]:
    statements: list[str] = []
    folder = resources.files("sre_agent") / "migrations"
    for file in sorted(
        (f for f in folder.iterdir() if f.name.endswith(".cql")), key=lambda f: f.name
    ):
        text = "\n".join(
            line.split("--")[0] for line in file.read_text().splitlines() if line.strip()
        )
        statements += [s.strip() for s in text.split(";") if s.strip()]
    return statements


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class CassandraStore:
    def __init__(self, session: Any) -> None:
        self._session = session
        self._prepared: dict[str, Any] = {}

    async def _run(self, fn: Callable[[], T]) -> T:
        return await asyncio.to_thread(fn)

    def _stmt(self, name: str) -> Any:
        if name not in self._prepared:
            self._prepared[name] = self._session.prepare(QUERIES[name])
        return self._prepared[name]

    def _execute(self, name: str, *args: Any) -> Any:
        return self._session.execute(self._stmt(name), args)

    async def migrate(self) -> None:
        def apply() -> None:
            for statement in migration_statements():
                self._session.execute(statement)

        await self._run(apply)

    async def next_incident_number(self) -> int:
        def take() -> int:
            for _ in range(20):  # a lost race (impossible with one replica) just tries again
                row = self._execute("counter").one()
                if row is None:
                    if self._execute("counter_first").one().applied:
                        return 1
                    continue
                if self._execute("counter_next", row.last + 1, row.last).one().applied:
                    return int(row.last + 1)
            raise RuntimeError("could not take an incident number")

        return await self._run(take)

    async def save(self, incident: Incident) -> None:
        doc = json.dumps(incident.to_document(), separators=(",", ":"))
        opened = incident.opened_at

        def write() -> None:
            self._execute("save", incident.incident_id, opened, doc)
            self._execute("index", opened.date(), opened, incident.incident_id)

        await self._run(write)

    async def get(self, incident_id: str) -> Incident | None:
        row = await self._run(lambda: self._execute("get", incident_id).one())
        return None if row is None else Incident.from_document(json.loads(row.doc))

    async def recent(self, limit: int, before: datetime | None = None) -> list[Incident]:
        def read() -> list[Incident]:
            start: date = (before or datetime.now(UTC)).date()
            ids: list[str] = []
            for back in range(LOOK_BACK_DAYS + 1):
                day = start - timedelta(days=back)
                left = limit - len(ids)
                if left <= 0:
                    break
                if before is not None and back == 0:
                    rows = self._execute("day_before", day, before, left)
                else:
                    rows = self._execute("day", day, left)
                ids += [r.incident_id for r in rows]
            out = []
            for incident_id in ids:
                row = self._execute("get", incident_id).one()
                if row is not None:
                    out.append(Incident.from_document(json.loads(row.doc)))
            return out

        return await self._run(read)

    async def settings(self, environment: str) -> AgentSettings | None:
        row = await self._run(lambda: self._execute("settings", environment).one())
        if row is None:
            return None
        return AgentSettings(
            environment=row.environment,
            approval_required=bool(row.approval_required),
            paused=bool(row.paused),
            updated_at=_utc(row.updated_at),
            updated_by=row.updated_by,
        )

    async def save_settings(self, settings: AgentSettings) -> None:
        s = settings
        await self._run(
            lambda: self._execute(
                "save_settings",
                s.environment,
                s.approval_required,
                s.paused,
                s.updated_at,
                s.updated_by,
            )
        )

    async def record_action(self, record: ActionRecord) -> None:
        r = record
        await self._run(
            lambda: self._execute("record", r.service, r.executed_at, r.incident_id, r.action)
        )

    async def actions_since(self, since: datetime) -> list[ActionRecord]:
        def read() -> list[ActionRecord]:
            out = []
            for service in sorted(APPLICATION_SERVICES):
                for row in self._execute("actions", service, since):
                    out.append(
                        ActionRecord(
                            row.service, _utc(row.executed_at), row.incident_id, row.action
                        )
                    )
            return out

        return await self._run(read)
