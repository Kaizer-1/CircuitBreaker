"""Where incidents, settings and the action log live (keyspace sre_agent, data ownership doc)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sre_agent.domain.incident import Incident


@dataclass(frozen=True)
class AgentSettings:
    environment: str
    approval_required: bool
    paused: bool
    updated_at: datetime
    updated_by: str | None


@dataclass(frozen=True)
class ActionRecord:
    service: str
    executed_at: datetime
    incident_id: str
    action: str


class Store(Protocol):
    async def next_incident_number(self) -> int: ...

    async def save(self, incident: Incident) -> None:
        """The whole incident, timeline included (written after every change)."""
        ...

    async def get(self, incident_id: str) -> Incident | None: ...

    async def recent(self, limit: int, before: datetime | None = None) -> list[Incident]:
        """Newest first, by opened_at."""
        ...

    async def settings(self, environment: str) -> AgentSettings | None: ...

    async def save_settings(self, settings: AgentSettings) -> None: ...

    async def record_action(self, record: ActionRecord) -> None: ...

    async def actions_since(self, since: datetime) -> list[ActionRecord]: ...
