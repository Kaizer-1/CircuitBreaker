"""Is the running pod template a new release, or one that already ran well? (ADR-0021)

Kubernetes keeps one ReplicaSet per pod template a Deployment ever ran (up to its history limit),
numbered by the `deployment.kubernetes.io/revision` annotation. Three things look alike in that
list and must not be confused:

- a **new release** (a new image or new settings): a template never seen before;
- a **rollback**: Kubernetes reuses the old ReplicaSet and gives it the newest number, so the
  revision is new but the template is old (known-good);
- a **restart**: a new ReplicaSet whose template differs only in the `restartedAt` annotation,
  the same release as before.

So a release is identified by its template **without** the restart annotation and the
pod-template-hash label (the release fingerprint), and its age is that of the OLDEST ReplicaSet
with that fingerprint.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sre_agent.domain.signals import NEW_REVISION_MINUTES

RESTARTED_AT = "kubectl.kubernetes.io/restartedAt"
TEMPLATE_HASH = "pod-template-hash"


@dataclass(frozen=True)
class ReplicaSetInfo:
    revision: int
    created_at: datetime
    template: dict[str, Any]  # spec.template as the API returns it
    change_cause: str | None = None  # the chart writes "release <version> (<tag>)"

    @property
    def images(self) -> tuple[str, ...]:
        containers = self.template.get("spec", {}).get("containers", [])
        return tuple(str(c.get("image", "")) for c in containers)


def release_fingerprint(template: dict[str, Any]) -> str:
    """The template with what a restart or the ReplicaSet itself adds left out."""
    metadata = dict(template.get("metadata") or {})
    labels = {k: v for k, v in (metadata.get("labels") or {}).items() if k != TEMPLATE_HASH}
    annotations = {
        k: v for k, v in (metadata.get("annotations") or {}).items() if k != RESTARTED_AT
    }
    canonical = {**template, "metadata": {**metadata, "labels": labels, "annotations": annotations}}
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:12]


@dataclass(frozen=True)
class RevisionFacts:
    revision: int
    release: str | None  # change-cause of the running revision
    first_ran_minutes: float  # how long ago this release's template first ran
    first_ran_at: datetime  # when its first ReplicaSet was made
    known_good: bool  # it already ran long before (a rollback to it, or a restart of it)
    rollback_to: int | None  # the newest revision of a DIFFERENT release, if any
    rollback_release: str | None

    @property
    def new(self) -> bool:
        return self.first_ran_minutes < NEW_REVISION_MINUTES and not self.known_good


def facts(replicasets: list[ReplicaSetInfo], now: datetime) -> RevisionFacts | None:
    """The running revision is the highest-numbered ReplicaSet (as `kubectl rollout` counts)."""
    if not replicasets:
        return None
    ordered = sorted(replicasets, key=lambda rs: rs.revision)
    current = ordered[-1]
    release = release_fingerprint(current.template)
    same = [rs for rs in ordered if release_fingerprint(rs.template) == release]
    first_ran = min(rs.created_at for rs in same)
    first_ran_minutes = max(0.0, (now - first_ran).total_seconds() / 60)
    earlier = [rs for rs in ordered if release_fingerprint(rs.template) != release]
    target = earlier[-1] if earlier else None
    return RevisionFacts(
        revision=current.revision,
        release=current.change_cause,
        first_ran_minutes=first_ran_minutes,
        first_ran_at=first_ran,
        known_good=first_ran_minutes >= NEW_REVISION_MINUTES,
        rollback_to=target.revision if target else None,
        rollback_release=target.change_cause if target else None,
    )


def history(
    replicasets: list[ReplicaSetInfo], now: datetime, last: int = 5
) -> list[dict[str, Any]]:
    """The deploy history for the model's evidence: newest first, restarts marked as such."""
    ordered = sorted(replicasets, key=lambda rs: rs.revision)
    seen: dict[str, int] = {}
    rows = []
    for rs in ordered:
        fingerprint = release_fingerprint(rs.template)
        rows.append(
            {
                "revision": rs.revision,
                "created_minutes_ago": round((now - rs.created_at).total_seconds() / 60, 1),
                "release": rs.change_cause,
                "images": list(rs.images),
                "same_release_as_revision": seen.get(fingerprint),
            }
        )
        seen.setdefault(fingerprint, rs.revision)
    return list(reversed(rows))[:last]
