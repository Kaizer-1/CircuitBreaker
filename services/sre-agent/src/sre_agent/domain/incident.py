"""One incident and its life (SRE agent design section 1): the aggregate behind Q-17/Q-18, the feed
and incidents.v1.

Every change is a method that returns the events it causes, as (event_type, data) pairs in the
event catalog's shape; the application layer wraps them in envelopes, stores the incident and
pushes the timeline entry to the feed. Times are kept as the approved timing definitions need
them (owner, 2026-11-24): the alert FIRED, the agent OPENED the incident (detect), MITIGATED (the
action ran), the alert stopped firing (RECOVERED), and RESOLVED after the 60-second check.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from sre_agent.domain.actions import APPLICATION_SERVICES, Proposal, Tool

Event = tuple[str, dict[str, Any]]


class Status(StrEnum):
    OPEN = "OPEN"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    RESOLVED = "RESOLVED"


class Resolution(StrEnum):
    RECOVERED = "RECOVERED"  # cleared after the agent's action
    AUTO_CLEARED = "AUTO_CLEARED"  # cleared with no action
    ESCALATED = "ESCALATED"  # handed to a person


class RemediationStatus(StrEnum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    DECLINED = "DECLINED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"


class Kind(StrEnum):
    OPENED = "OPENED"
    EVIDENCE_COLLECTED = "EVIDENCE_COLLECTED"
    DIAGNOSIS = "DIAGNOSIS"
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    DECLINED = "DECLINED"
    EXECUTED = "EXECUTED"
    VERIFIED = "VERIFIED"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"
    AFTER_EFFECT = "AFTER_EFFECT"  # a backlog alert after a fix: draining, drained, handed over


def iso(at: datetime | None) -> str | None:
    if at is None:
        return None
    return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def git_line(git: dict[str, Any]) -> str:
    """The rollback's Git step (ADR-0016) as one feed line: the screen shows only the timeline."""
    reason = str(git.get("reason") or "").strip()
    if git.get("status") == "PR_OPENED":
        return f"Git: {reason} {git.get('pull_request_url')} (a person merges it)."
    if git.get("status") == "NOT_NEEDED":
        return f"Git: no revert needed. {reason}"
    return f"Git: {reason}"


def parse_time(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))


class NotPendingError(Exception):
    """A decision for a remediation that is not waiting for one (A-15's 409)."""


@dataclass
class Remediation:
    remediation_id: str
    action: str
    target_service: str | None
    parameters: dict[str, Any]
    explanation: str
    model: str  # the model's slug, or "rule-based (LLM unavailable)", or "fixed rule"
    status: RemediationStatus
    requires_approval: bool
    proposed_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    executed_at: datetime | None = None
    git_reconciliation: dict[str, Any] | None = None
    details: str | None = None

    def to_api(self) -> dict[str, Any]:
        return {
            "remediation_id": self.remediation_id,
            "action": self.action,
            "target_service": self.target_service,
            "parameters": self.parameters,
            "explanation": self.explanation,
            "model": self.model,
            "status": self.status.value,
            "requires_approval": self.requires_approval,
            "proposed_at": iso(self.proposed_at),
            "decided_at": iso(self.decided_at),
            "decided_by": self.decided_by,
            "executed_at": iso(self.executed_at),
            "git_reconciliation": self.git_reconciliation,
        }

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Remediation":
        return cls(
            remediation_id=d["remediation_id"],
            action=d["action"],
            target_service=d["target_service"],
            parameters=d["parameters"],
            explanation=d["explanation"],
            model=d["model"],
            status=RemediationStatus(d["status"]),
            requires_approval=d["requires_approval"],
            proposed_at=parse_time(d["proposed_at"]),  # type: ignore[arg-type]
            decided_at=parse_time(d["decided_at"]),
            decided_by=d["decided_by"],
            executed_at=parse_time(d["executed_at"]),
            git_reconciliation=d.get("git_reconciliation"),
            details=d.get("details"),
        )


@dataclass(frozen=True)
class Entry:
    at: datetime
    kind: Kind
    message: str

    def to_api(self) -> dict[str, Any]:
        return {"at": iso(self.at), "kind": self.kind.value, "message": self.message}


@dataclass
class Incident:
    incident_id: str
    alert_name: str
    service: str
    severity: str
    summary: str
    opened_at: datetime
    fired_at: datetime | None = None
    status: Status = Status.OPEN
    resolution: Resolution | None = None
    resolved_at: datetime | None = None
    mitigated_at: datetime | None = None
    recovered_at: datetime | None = None
    evidence_summary: str = ""
    remediations: list[Remediation] = field(default_factory=list)
    timeline: list[Entry] = field(default_factory=list)

    # ------------------------------------------------------------------ reading

    @property
    def actions_taken(self) -> list[str]:
        """Actions that ran (do_nothing is a decision, not an action)."""
        return [
            r.action
            for r in self.remediations
            if r.status is RemediationStatus.EXECUTED and r.action != Tool.NOTHING
        ]

    @property
    def pending(self) -> Remediation | None:
        return next((r for r in self.remediations if r.status is RemediationStatus.PROPOSED), None)

    @property
    def time_to_recover_seconds(self) -> int | None:
        """From the incident opening to the alert clearing (MTTR's source)."""
        end = self.recovered_at or self.resolved_at
        if end is None or self.resolution is Resolution.ESCALATED:
            return None
        return max(0, round((end - self.opened_at).total_seconds()))

    def find(self, remediation_id: str) -> Remediation:
        for r in self.remediations:
            if r.remediation_id == remediation_id:
                return r
        raise NotPendingError(f"Remediation {remediation_id} is not part of {self.incident_id}.")

    def to_summary(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "alert_name": self.alert_name,
            "service": self.service,
            "severity": self.severity,
            "status": self.status.value,
            "resolution": self.resolution.value if self.resolution else None,
            "summary": self.summary,
            "opened_at": iso(self.opened_at),
            "resolved_at": iso(self.resolved_at),
            "time_to_recover_seconds": self.time_to_recover_seconds,
            "actions_taken": self.actions_taken,
            "fired_at": iso(self.fired_at),
            "mitigated_at": iso(self.mitigated_at),
            "recovered_at": iso(self.recovered_at),
        }

    def to_document(self) -> dict[str, Any]:
        """Everything, for the store: the API view plus what only the agent needs."""
        return {
            **self.to_api(),
            "remediation_details": {r.remediation_id: r.details for r in self.remediations},
        }

    @classmethod
    def from_document(cls, d: dict[str, Any]) -> "Incident":
        details = d.get("remediation_details", {})
        remediations = []
        for raw in d["remediations"]:
            r = Remediation.from_api(raw)
            r.details = details.get(r.remediation_id)
            remediations.append(r)
        return cls(
            incident_id=d["incident_id"],
            alert_name=d["alert_name"],
            service=d["service"],
            severity=d["severity"],
            summary=d["summary"],
            opened_at=parse_time(d["opened_at"]),  # type: ignore[arg-type]
            fired_at=parse_time(d.get("fired_at")),
            status=Status(d["status"]),
            resolution=Resolution(d["resolution"]) if d.get("resolution") else None,
            resolved_at=parse_time(d.get("resolved_at")),
            mitigated_at=parse_time(d.get("mitigated_at")),
            recovered_at=parse_time(d.get("recovered_at")),
            evidence_summary=d.get("evidence_summary", ""),
            remediations=remediations,
            timeline=[
                Entry(parse_time(e["at"]), Kind(e["kind"]), e["message"])  # type: ignore[arg-type]
                for e in d.get("timeline", [])
            ],
        )

    def to_api(self) -> dict[str, Any]:
        pending = self.pending
        return {
            **self.to_summary(),
            "evidence_summary": self.evidence_summary,
            "pending_remediation": pending.to_api() if pending else None,
            "remediations": [r.to_api() for r in self.remediations],
            "timeline": [e.to_api() for e in self.timeline],
        }

    # ------------------------------------------------------------------ changing

    @classmethod
    def open(
        cls,
        number: int,
        alert_name: str,
        service: str,
        severity: str,
        summary: str,
        at: datetime,
        fired_at: datetime | None = None,
    ) -> tuple["Incident", list[Event]]:
        incident = cls(f"INC-{number:04d}", alert_name, service, severity, summary, at, fired_at)
        incident.note(Kind.OPENED, summary, at)
        data = {
            "incident_id": incident.incident_id,
            "alert_name": alert_name,
            "service": service,
            "severity": severity,
            "summary": summary,
            "opened_at": iso(at),
        }
        return incident, [("IncidentOpened", data)]

    def note(self, kind: Kind, message: str, at: datetime) -> Entry:
        entry = Entry(at, kind, message)
        self.timeline.append(entry)
        return entry

    def propose(
        self,
        p: Proposal,
        explanation: str,
        parameters: dict[str, Any],
        requires_approval: bool,
        at: datetime,
    ) -> tuple[Remediation, list[Event]]:
        """A proposal that passed the guardrails."""
        r = Remediation(
            remediation_id=str(uuid4()),
            action=p.tool,
            target_service=p.service,
            parameters=parameters,
            explanation=explanation,
            model=p.source,
            status=RemediationStatus.PROPOSED,
            requires_approval=requires_approval,
            proposed_at=at,
        )
        self.remediations.append(r)
        if requires_approval:
            self.status = Status.AWAITING_APPROVAL
        self.note(Kind.PROPOSED, f"Proposed: {describe(r)}.", at)
        data = {
            "incident_id": self.incident_id,
            "remediation_id": r.remediation_id,
            "action": r.action,
            "target_service": r.target_service,
            "parameters": parameters,
            "explanation": explanation,
            "model": r.model,
            "requires_approval": requires_approval,
            "proposed_at": iso(at),
        }
        return r, [("RemediationProposed", data)]

    def decline_proposal(
        self, p: Proposal, explanation: str, reason: str, at: datetime
    ) -> tuple[Remediation, list[Event]]:
        """A proposal the guardrails refused: recorded, never run."""
        r = Remediation(
            remediation_id=str(uuid4()),
            action=p.tool if p.tool in {t.value for t in Tool} else Tool.NOTHING.value,
            target_service=p.service if p.service in APPLICATION_SERVICES else None,
            parameters={"replicas": p.replicas} if p.replicas in range(1, 6) else {},
            explanation=explanation,
            model=p.source,
            status=RemediationStatus.PROPOSED,
            requires_approval=False,
            proposed_at=at,
        )
        self.remediations.append(r)
        self.note(Kind.DIAGNOSIS, f"Wanted: {p.summary}.", at)
        return r, self.decline(r.remediation_id, reason, at)

    def approve(
        self, remediation_id: str, by: str, comment: str | None, at: datetime
    ) -> list[Event]:
        r = self._pending(remediation_id)
        r.status, r.decided_at, r.decided_by = RemediationStatus.APPROVED, at, by
        self.status = Status.OPEN
        self.note(Kind.APPROVED, f"Approved by {by}." + (f" {comment}" if comment else ""), at)
        data = {
            "incident_id": self.incident_id,
            "remediation_id": remediation_id,
            "decided_by": by,
            "comment": comment,
            "decided_at": iso(at),
        }
        return [("RemediationApproved", data)]

    def decline(
        self, remediation_id: str, reason: str, at: datetime, admin: str | None = None
    ) -> list[Event]:
        """By an admin (A-15: `admin` is their nickname), else by a guardrail (including the
        approval timing out)."""
        r = self._pending(remediation_id)
        r.status, r.decided_at = RemediationStatus.DECLINED, at
        r.decided_by = admin or "guardrail"
        self.status = Status.OPEN
        who = f"by {admin}" if admin else "by a guardrail"
        self.note(Kind.DECLINED, f"Declined {who}: {reason}", at)
        data = {
            "incident_id": self.incident_id,
            "remediation_id": remediation_id,
            "declined_by": "admin" if admin else "guardrail",
            "reason": reason,
            "declined_at": iso(at),
        }
        return [("RemediationDeclined", data)]

    def executed(
        self,
        remediation_id: str,
        succeeded: bool,
        details: str,
        at: datetime,
        git_reconciliation: dict[str, Any] | None = None,
    ) -> list[Event]:
        r = self.find(remediation_id)
        r.status = RemediationStatus.EXECUTED if succeeded else RemediationStatus.FAILED
        r.executed_at, r.details, r.git_reconciliation = at, details, git_reconciliation
        if succeeded and r.action != Tool.NOTHING and self.mitigated_at is None:
            self.mitigated_at = at
        self.note(Kind.EXECUTED, details, at)
        if git_reconciliation and r.action == Tool.ROLLBACK:
            self.note(Kind.EXECUTED, git_line(git_reconciliation), at)
        data = {
            "incident_id": self.incident_id,
            "remediation_id": remediation_id,
            "action": r.action,
            "target_service": r.target_service,
            "parameters": r.parameters,
            "outcome": "SUCCEEDED" if succeeded else "FAILED",
            "details": details,
            "executed_at": iso(at),
            "git_reconciliation": git_reconciliation if r.action == Tool.ROLLBACK else None,
        }
        return [("RemediationExecuted", data)]

    def resolve(self, resolution: Resolution, message: str, at: datetime) -> list[Event]:
        if self.status is Status.RESOLVED:
            return []
        pending = self.pending
        events: list[Event] = []
        if pending is not None:  # never left waiting after the incident ends
            events += self.decline(pending.remediation_id, message, at)
        self.status, self.resolution, self.resolved_at = Status.RESOLVED, resolution, at
        kind = Kind.ESCALATED if resolution is Resolution.ESCALATED else Kind.RESOLVED
        self.note(kind, message, at)
        data = {
            "incident_id": self.incident_id,
            "service": self.service,
            "resolution": resolution.value,
            "opened_at": iso(self.opened_at),
            "resolved_at": iso(at),
            "time_to_recover_seconds": self.time_to_recover_seconds or 0,
            "actions_taken": self.actions_taken,
        }
        return [*events, ("IncidentResolved", data)]

    def _pending(self, remediation_id: str) -> Remediation:
        r = self.find(remediation_id)
        if r.status is not RemediationStatus.PROPOSED:
            raise NotPendingError(
                f"Remediation {remediation_id} was already {r.status.value.lower()}."
            )
        return r


def describe(r: Remediation) -> str:
    """The remediation as the feed shows it."""
    match r.action:
        case Tool.SCALE:
            return f"scale {r.target_service} to {r.parameters.get('replicas')} replicas"
        case Tool.ROLLBACK:
            to = r.parameters.get("to_revision")
            return f"roll back {r.target_service}" + (f" to revision {to}" if to else "")
        case Tool.RESTART:
            return f"restart {r.target_service}"
        case _:
            return "do nothing"
