"""The guardrails (SRE agent design section 5): ordinary code that decides whether a proposed tool
call may run. The model only proposes; this decides. Checked in this order, the first failure wins:

G8  paused by an admin            -> record the proposal, never execute
G1  the tool is one of the four   -> decline
G2  the target is allowed for it  -> decline (data stores are in no list)
G3  replicas 1..5, step <= 3      -> decline
G7  rollback needs an older revision -> decline
G5  5-minute cooldown per service -> decline and escalate
    one action at a time per service -> decline and escalate
G6  3 executed actions per incident, 6 per hour -> escalate
G10 explanation 1..600 characters -> replace it with a plain one and flag it (not a decline)

G4 (own namespace only) is the Kubernetes Role's job; the tools take no namespace at all. G9 (the
model failed) is the caller's: it falls back to the rules (ADR-0021), whose proposals come here too.
G11 (the rollback's Git step) is checked inside rollback_deployment.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sre_agent.domain.actions import MAX_REPLICAS, MAX_STEP, TARGETS, Proposal, Tool

COOLDOWN = timedelta(minutes=5)
PER_INCIDENT = 3
PER_HOUR = 6
EXPLANATION_MAX = 600


@dataclass(frozen=True)
class Situation:
    """What the guardrails need to know, gathered by the agent's own code (never by the model)."""

    now: datetime
    paused: bool = False
    executed_this_incident: int = 0
    executed_last_hour: int = 0
    last_action_at: dict[str, datetime] = field(default_factory=dict)  # per service
    acting_on: frozenset[str] = frozenset()  # services with an action still being verified
    replicas: dict[str, int] = field(default_factory=dict)
    has_previous_revision: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    guardrail: str | None  # which one said no (None: allowed)
    reason: str  # plain English, shown on the incident feed
    escalate: bool
    explanation: str  # the proposal's, or a plain replacement (G10)
    flagged: bool = False  # the explanation was replaced


def _no(guardrail: str, reason: str, explanation: str, *, escalate: bool = False) -> Verdict:
    return Verdict(False, guardrail, reason, escalate, explanation)


def check(p: Proposal, s: Situation) -> Verdict:
    explanation, flagged = p.explanation.strip(), False
    if not 1 <= len(explanation) <= EXPLANATION_MAX:  # G10
        explanation, flagged = f"The agent proposed {p.summary}.", True

    if s.paused:  # G8
        return _no("G8", "The agent is paused: proposals are recorded, never run.", explanation)
    if p.tool not in {t.value for t in Tool}:  # G1
        return _no("G1", f"{p.tool!r} is not one of the four tools.", explanation, escalate=True)
    tool = Tool(p.tool)
    if tool is Tool.NOTHING:
        return Verdict(True, None, "Nothing to run.", False, explanation, flagged)
    if p.service not in TARGETS[tool]:  # G2
        return _no(
            "G2",
            f"{tool.value} may not touch {p.service!r}: it is not on that tool's allowlist"
            " (data stores never are).",
            explanation,
            escalate=True,
        )
    if tool is Tool.SCALE:  # G3
        current = s.replicas.get(p.service, 0)
        if p.replicas is None or not 1 <= p.replicas <= MAX_REPLICAS:
            return _no(
                "G3", f"Replicas must be 1 to {MAX_REPLICAS}, not {p.replicas}.", explanation
            )
        if abs(p.replicas - current) > MAX_STEP:
            return _no(
                "G3",
                f"One step changes replicas by at most {MAX_STEP} ({current} to {p.replicas}).",
                explanation,
            )
        if p.replicas == current:
            return _no("G3", f"{p.service} already runs {current} replicas.", explanation)
    if tool is Tool.ROLLBACK and not s.has_previous_revision.get(p.service, False):  # G7
        return _no("G7", f"{p.service} has no earlier revision to roll back to.", explanation)
    if p.service in s.acting_on:
        return _no(
            "G5",
            f"Another action on {p.service} is still being verified: one at a time.",
            explanation,
            escalate=True,
        )
    last = s.last_action_at.get(p.service)
    if last is not None and s.now - last < COOLDOWN:  # G5
        minutes = (s.now - last).total_seconds() / 60
        return _no(
            "G5",
            f"Cooldown active: the agent acted on {p.service} {minutes:.0f} min ago"
            f" (the cooldown is {COOLDOWN.seconds // 60} min).",
            explanation,
            escalate=True,
        )
    if s.executed_this_incident >= PER_INCIDENT:  # G6
        return _no(
            "G6", f"{PER_INCIDENT} actions on this incident already.", explanation, escalate=True
        )
    if s.executed_last_hour >= PER_HOUR:
        return _no(
            "G6", f"{PER_HOUR} actions in the last hour already.", explanation, escalate=True
        )
    return Verdict(True, None, "Within every guardrail.", False, explanation, flagged)
