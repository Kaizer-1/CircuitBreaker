"""Is the running pod template a new release, or one that already ran well? (ADR-0021)

Kubernetes keeps one ReplicaSet per pod template a Deployment ever ran (up to its history limit),
numbered by the `deployment.kubernetes.io/revision` annotation. Things that look alike in that
list must not be confused:

- a **new release** (a new image or new settings): a template never run for long;
- a **rollback**: Kubernetes reuses the old ReplicaSet and gives it the newest number, so the
  revision number is new but the template ran for hours before (known-good);
- a **bad release deployed again**: Kubernetes reuses ITS old ReplicaSet too, so its creation
  time is old although it never ran well (found on staging, 2026-11-26: the second chaos
  BAD_DEPLOY of the day reused the first one's ReplicaSet and looked known-good);
- a **restart**: a new ReplicaSet whose template differs only in the `restartedAt` annotation,
  the same release as before.

So a release is identified by its template **without** the restart annotation and the
pod-template-hash label (the release fingerprint), and it is **known-good** when it once RAN
(had ready pods) for at least 15 minutes in a row: Prometheus keeps each ReplicaSet's ready
replicas for 12 hours. A release's ReplicaSets count together, so a restart does not make it new.
Without that history (Prometheus not answering), the age of its oldest ReplicaSet stands in.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sre_agent.domain.signals import NEW_REVISION_MINUTES

RESTARTED_AT = "kubectl.kubernetes.io/restartedAt"
TEMPLATE_HASH = "pod-template-hash"
SAME_RUN_GAP = timedelta(minutes=2)  # a restart's hand-over, or a missing sample, is one run

Span = tuple[datetime, datetime]


@dataclass(frozen=True)
class ReplicaSetInfo:
    revision: int
    created_at: datetime
    template: dict[str, Any]  # spec.template as the API returns it
    change_cause: str | None = None  # the chart writes "release <version> (<tag>)"
    name: str = ""

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


def spans_from_series(points: list[float | None], end: datetime, step_seconds: int) -> list[Span]:
    """Where a ReplicaSet had ready pods, from its series (oldest first, last point at `end`)."""
    spans: list[Span] = []
    start: datetime | None = None
    step = timedelta(seconds=step_seconds)
    for i, value in enumerate(points):
        at = end - step * (len(points) - 1 - i)
        if value and value > 0:
            start = start or at
        elif start is not None:
            spans.append((start, at - step))
            start = None
    if start is not None:
        spans.append((start, end))
    return spans


def merge(spans: list[Span]) -> list[Span]:
    merged: list[Span] = []
    for s, e in sorted(spans):
        if merged and s - merged[-1][1] <= SAME_RUN_GAP:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


@dataclass(frozen=True)
class RevisionFacts:
    revision: int
    release: str | None  # change-cause of the running revision
    running_minutes: float  # how long this release has been running, this time
    longest_run_minutes: float  # its longest run in a row (12 h of history), this one included
    first_ran_at: datetime  # when this run began (when its template went live this time)
    rollback_to: int | None  # the newest revision of a DIFFERENT release, if any
    rollback_release: str | None

    @property
    def known_good(self) -> bool:
        return self.longest_run_minutes >= NEW_REVISION_MINUTES

    @property
    def new(self) -> bool:
        return not self.known_good


def facts(
    replicasets: list[ReplicaSetInfo],
    now: datetime,
    activity: dict[str, list[Span]] | None = None,
) -> RevisionFacts | None:
    """The running revision is the highest-numbered ReplicaSet (as `kubectl rollout` counts).
    `activity`: each ReplicaSet's runs (by name), from Prometheus; None or empty: not known."""
    if not replicasets:
        return None
    ordered = sorted(replicasets, key=lambda rs: rs.revision)
    current = ordered[-1]
    release = release_fingerprint(current.template)
    same = [rs for rs in ordered if release_fingerprint(rs.template) == release]
    runs = merge([span for rs in same for span in (activity or {}).get(rs.name, [])])
    if activity and not runs:
        # History is known, and this release never had a ready pod in it: it never ran at all
        # (a bad release whose pods crash at start, deployed now or again).
        began = max(rs.created_at for rs in same)
        running = longest = 0.0
    elif runs:
        live = runs[-1] if now - runs[-1][1] <= SAME_RUN_GAP else None
        began = live[0] if live else now
        # A ReplicaSet made during this run says to the second when it began (the series only to
        # the minute); a reused one cannot, so the run's first sample stands.
        fresh = [rs.created_at for rs in same if rs.created_at >= began - SAME_RUN_GAP]
        began = min(fresh) if fresh else began
        running = max(0.0, (now - began).total_seconds() / 60)
        longest = max(running, *((e - s).total_seconds() / 60 for s, e in runs))
    else:
        began = min(rs.created_at for rs in same)
        running = longest = max(0.0, (now - began).total_seconds() / 60)
    earlier = [rs for rs in ordered if release_fingerprint(rs.template) != release]
    target = earlier[-1] if earlier else None
    return RevisionFacts(
        revision=current.revision,
        release=current.change_cause,
        running_minutes=running,
        longest_run_minutes=longest,
        first_ran_at=began,
        rollback_to=target.revision if target else None,
        rollback_release=target.change_cause if target else None,
    )


CONTAINER_FIELDS = (
    "image",
    "command",
    "args",
    "env",
    "envFrom",
    "resources",
    "ports",
    "readinessProbe",
    "livenessProbe",
    "startupProbe",
    "volumeMounts",
)


def template_changes(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """What differs between two pod templates, in a few words ("order-service: resources").
    The release name (change-cause) can stay the same while the template changes: the chart names a
    release by version and image tag, so a new memory limit keeps the name (staging, 2026-11-27)."""
    changes: list[str] = []
    a_spec, b_spec = before.get("spec", {}), after.get("spec", {})
    a_c = {c.get("name"): c for c in a_spec.get("containers", [])}
    b_c = {c.get("name"): c for c in b_spec.get("containers", [])}
    for name in sorted(set(a_c) | set(b_c), key=str):
        if name not in a_c or name not in b_c:
            changes.append(f"container {name} {'added' if name in b_c else 'removed'}")
            continue
        fields = [f for f in CONTAINER_FIELDS if a_c[name].get(f) != b_c[name].get(f)]
        if fields:
            changes.append(f"{name}: {', '.join(fields)}")
    for field in ("volumes", "serviceAccountName", "nodeSelector"):
        if a_spec.get(field) != b_spec.get(field):
            changes.append(field)

    def notes(t: dict[str, Any]) -> dict[str, Any]:
        annotations = (t.get("metadata") or {}).get("annotations") or {}
        return {k: v for k, v in annotations.items() if k != RESTARTED_AT}

    changed = sorted(
        k
        for k in set(notes(before)) | set(notes(after))
        if notes(before).get(k) != notes(after).get(k)
    )
    changes += [f"annotation {k}" for k in changed]
    return changes


def history(
    replicasets: list[ReplicaSetInfo], now: datetime, last: int = 5
) -> list[dict[str, Any]]:
    """The deploy history for the model's evidence: newest first, restarts marked as such, and
    what each revision changed from the one before."""
    ordered = sorted(replicasets, key=lambda rs: rs.revision)
    seen: dict[str, int] = {}
    rows = []
    previous: ReplicaSetInfo | None = None
    for rs in ordered:
        fingerprint = release_fingerprint(rs.template)
        rows.append(
            {
                "revision": rs.revision,
                "created_minutes_ago": round((now - rs.created_at).total_seconds() / 60, 1),
                "release": rs.change_cause,
                "images": list(rs.images),
                "same_release_as_revision": seen.get(fingerprint),
                "changed_from_previous": (
                    template_changes(previous.template, rs.template) if previous else None
                ),
            }
        )
        seen.setdefault(fingerprint, rs.revision)
        previous = rs
    return list(reversed(rows))[:last]
