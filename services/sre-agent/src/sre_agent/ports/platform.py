"""The agent's hands: one Deployment at a time, through the Kubernetes API (A-17), with exactly
what its Role allows (get/list/patch Deployments, their scale, read ReplicaSets and Pods)."""

from dataclasses import dataclass, field
from typing import Protocol

from sre_agent.domain.revisions import ReplicaSetInfo


class PlatformError(Exception):
    """The Kubernetes API refused or did not answer."""


@dataclass(frozen=True)
class PodInfo:
    name: str
    phase: str
    ready: bool
    restarts: int
    last_termination: str | None  # e.g. "OOMKilled"
    revision: int | None = None  # the Deployment revision (ReplicaSet) it belongs to


@dataclass(frozen=True)
class DeploymentState:
    name: str
    desired: int
    ready: int
    updated: int
    replicasets: list[ReplicaSetInfo] = field(default_factory=list)
    pods: list[PodInfo] = field(default_factory=list)

    @property
    def rolled_out(self) -> bool:
        return self.updated == self.desired and self.ready == self.desired


class Platform(Protocol):
    async def deployment(self, service: str) -> DeploymentState: ...

    async def rollback(self, service: str, to_revision: int) -> str:
        """Back to that revision's pod template and its change-cause (`kubectl rollout undo`)."""
        ...

    async def scale(self, service: str, replicas: int) -> str: ...

    async def restart(self, service: str) -> str:
        """New pods, same release (`kubectl rollout restart`)."""
        ...
