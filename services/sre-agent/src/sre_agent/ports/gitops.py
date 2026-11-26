"""The rollback's Git step (ADR-0016): deterministic code inside rollback_deployment, never a tool.

Before the rollback the agent reads the service's Argo CD Application (read-only account): if it
was Synced, the bad release came from Git, and after the rollback a `git revert` pull request is
opened by the GitHub App, which can never merge it. If it was OutOfSync, the bad release was live
drift (a chaos BAD_DEPLOY), and the rollback already brings the cluster back to what Git says."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class Sync:
    """One sync Argo CD ran (its Application's history): what it applied and when it began."""

    revision: str
    started_at: datetime


@dataclass(frozen=True)
class SyncState:
    synced: bool
    revision: str | None  # the Git commit Argo CD compared with
    detail: str
    branch: str | None = None  # the branch the Application follows (main, or a branch under test)
    history: tuple[Sync, ...] = ()  # oldest first


def sync_behind(history: tuple[Sync, ...], first_ran: datetime) -> tuple[Sync | None, Sync | None]:
    """The sync that put the bad pod template in place, and the one before it.

    A release from Git is a sync that STARTED shortly before the template first ran (Argo CD
    applies, then Kubernetes makes the ReplicaSet within seconds). "Synced" alone does not say
    that: Argo CD compares only the fields Git sets, and its automated sync may re-apply Git a few
    seconds AFTER a change made outside Git, then report Synced while that change still runs
    (found on staging, 2026-11-26)."""
    earlier = [s for s in history if s.started_at <= first_ran]
    if not earlier or (first_ran - earlier[-1].started_at).total_seconds() > SYNC_WINDOW_SECONDS:
        return None, None
    return earlier[-1], (earlier[-2] if len(earlier) > 1 else None)


SYNC_WINDOW_SECONDS = 180


class GitOps(Protocol):
    async def sync_state(self, service: str) -> SyncState | None:
        """None: unknown (Argo CD did not answer, or the service is not managed by it)."""
        ...

    async def reconcile(
        self,
        service: str,
        before: SyncState | None,
        incident_id: str,
        first_ran: datetime | None,
    ) -> dict[str, Any]:
        """The event catalog's git_reconciliation: PR_OPENED, NOT_NEEDED or FAILED (+ reason).
        `first_ran`: when the bad pod template first ran (its ReplicaSet was made)."""
        ...


class NoGitOps:
    """Docker Compose, or Argo CD not configured: nothing to reconcile."""

    async def sync_state(self, service: str) -> SyncState | None:
        return None

    async def reconcile(
        self,
        service: str,
        before: SyncState | None,
        incident_id: str,
        first_ran: datetime | None,
    ) -> dict[str, Any]:
        return {
            "status": "NOT_NEEDED",
            "reverted_commit": None,
            "pull_request_url": None,
            "reason": "This environment is not deployed by Argo CD: there is no Git state to fix.",
        }
