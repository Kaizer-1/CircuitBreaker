"""The rollback's Git step (ADR-0016): deterministic code inside rollback_deployment, never a tool.

Before the rollback the agent reads the service's Argo CD Application (read-only account): if it
was Synced, the bad release came from Git, and after the rollback a `git revert` pull request is
opened by the GitHub App, which can never merge it. If it was OutOfSync, the bad release was live
drift (a chaos BAD_DEPLOY), and the rollback already brings the cluster back to what Git says."""

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class SyncState:
    synced: bool
    revision: str | None  # the Git commit Argo CD synced, when Synced
    detail: str


class GitOps(Protocol):
    async def sync_state(self, service: str) -> SyncState | None:
        """None: unknown (Argo CD did not answer, or the service is not managed by it)."""
        ...

    async def reconcile(
        self, service: str, before: SyncState | None, incident_id: str
    ) -> dict[str, Any]:
        """The event catalog's git_reconciliation: PR_OPENED, NOT_NEEDED or FAILED (+ reason)."""
        ...


class NoGitOps:
    """Docker Compose, or Argo CD not configured: nothing to reconcile."""

    async def sync_state(self, service: str) -> SyncState | None:
        return None

    async def reconcile(
        self, service: str, before: SyncState | None, incident_id: str
    ) -> dict[str, Any]:
        return {
            "status": "NOT_NEEDED",
            "reverted_commit": None,
            "pull_request_url": None,
            "reason": "This environment is not deployed by Argo CD: there is no Git state to fix.",
        }
