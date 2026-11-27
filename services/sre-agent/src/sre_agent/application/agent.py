"""The agent loop (SRE agent design section 1): observe, decide, guard, (approve), act, verify.

Every 5 seconds the loop reads the firing alerts of its own namespace. A service with a firing
alert and no open incident gets one (one open incident per service, owner's decision 2026-11-24),
and that incident runs in its own task, so a slow model never delays noticing the next one:

    collect evidence -> decide (model, or rules: ADR-0021) -> guardrails -> approval if on
    -> execute through the Kubernetes API -> verify (the alert clear for 60 s)

at most three rounds, then a person takes over (ESCALATED). An escalated alert opens no new
incident while it keeps firing: a person already has it.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sre_agent.application.collector import GATEWAY, Collected, EvidenceCollector
from sre_agent.application.decider import Decider, Decision
from sre_agent.application.feed import FeedHub
from sre_agent.business_metrics import BusinessMetrics
from sre_agent.domain import guardrails
from sre_agent.domain.actions import APPLICATION_SERVICES, Tool
from sre_agent.domain.fallback import PASSED_ON
from sre_agent.domain.guardrails import Situation
from sre_agent.domain.incident import (
    Event,
    Incident,
    Kind,
    NotPendingError,
    Remediation,
    Resolution,
    Status,
)
from sre_agent.ports.events import EventSink
from sre_agent.ports.gitops import GitOps
from sre_agent.ports.observability import Alert, Metrics
from sre_agent.ports.platform import Platform, PlatformError
from sre_agent.ports.store import ActionRecord, AgentSettings, Store
from sre_agent.runtime.events import build_event

logger = logging.getLogger("agent")

# Which alert leads when one service has several: an open breaker explains everything after it.
LEAD_ORDER = (
    "OrderServiceCircuitOpen",
    "OrderServiceHighErrorRate",
    "PodCrashLooping",
    "MarketDataMemoryGrowth",
    "OrderServiceHighLatency",
    "PortfolioHighErrorRate",
    "GatewayHighErrorRate",
    "OrderServiceOverProvisioned",
)


def lead_alert(alerts: list[Alert]) -> Alert:
    def rank(a: Alert) -> tuple[int, int, str]:
        if a.name in LEAD_ORDER:
            return (0, LEAD_ORDER.index(a.name), a.name)
        return (1 if a.remediable else 2, 0, a.name)

    return min(alerts, key=rank)


# Only an alert that measures a backlog can be an after-effect: a backlog drains by itself once the
# fix works. An open circuit breaker or a dead-lettered message is a new problem whenever it comes
# (the live demo's database decline starts minutes after the panic sell's scale-up: found while
# planning it, 2026-11-27).
AFTER_EFFECT_ALERTS = frozenset({"ConsumerLagHigh"})


@dataclass
class AfterEffect:
    """A backlog alert expected after a fix, watched on the incident it follows."""

    incident: Incident
    since: datetime
    ends: datetime
    samples: list[tuple[datetime, float | None]]

    def stalled(self, now: datetime, seconds: float) -> tuple[float, float] | None:
        """(then, now) if the alert's value went no lower over the last `seconds`."""
        current = self.samples[-1][1]
        earlier = [v for t, v in self.samples if (now - t).total_seconds() >= seconds]
        if current is None or not earlier or earlier[-1] is None:
            return None
        return (earlier[-1], current) if current >= earlier[-1] else None


@dataclass(frozen=True)
class AgentConfig:
    environment: str
    approval_by_default: bool
    poll_seconds: float = 5.0
    approval_timeout_seconds: float = 300.0
    hold_seconds: float = 60.0  # the alert must stay clear this long to count as recovered
    max_rounds: int = 3
    # After a successful fix, a backlog alert on the same service (ConsumerLagHigh while the backlog
    # of a panic drains: AFTER_EFFECT_ALERTS) is an expected after-effect of THAT incident for this
    # long, not a new incident; it is handed to a person if it stops improving (owner, 2026-11-27).
    after_effect_seconds: float = 600.0
    drain_check_seconds: float = 60.0  # it must have gone down over this long
    # The gateway's error alert, when its errors are an upstream's answers passed on, is a SYMPTOM
    # of that upstream's incident (owner, 2026-11-27): a line on the root incident, not an incident
    # of its own. Fired before the root's incident opened: it waits this long for it.
    symptom_wait_seconds: float = 60.0
    # Still firing this long after its root incident was resolved: handed to a person.
    symptom_grace_seconds: float = 60.0
    # How long to wait for the alert to clear after each kind of action. Scaling is measured: new
    # pods take 20-60 s to be ready, traffic moves over within ~60 s more, and the latency alert
    # clears 22-28 s after the real recovery (chaos scenarios 3.3), so it gets 4 minutes.
    verify_seconds: dict[str, float] = field(
        default_factory=lambda: {
            Tool.ROLLBACK.value: 180.0,
            Tool.RESTART.value: 180.0,
            Tool.SCALE.value: 240.0,
            Tool.NOTHING.value: 120.0,
        }
    )


class Agent:
    def __init__(
        self,
        config: AgentConfig,
        *,
        alerts: Metrics,
        collector: EvidenceCollector,
        decider: Decider,
        platform: Platform,
        gitops: GitOps,
        store: Store,
        sink: EventSink | None,
        feed: FeedHub,
        now: Callable[[], datetime],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        metrics: BusinessMetrics | None = None,
    ) -> None:
        self._cfg = config
        self._alerts = alerts
        self._collector = collector
        self._decider = decider
        self._platform = platform
        self._gitops = gitops
        self._store = store
        self._sink = sink
        self.feed = feed
        self._now = now
        self._sleep = sleep
        self._metrics = metrics
        self._open: dict[str, Incident] = {}  # by service
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._escalated: set[tuple[str, str]] = set()  # (alert, service) a person already has
        self._decisions: dict[str, asyncio.Future[bool]] = {}
        self._actions: list[ActionRecord] = []
        self._acting: set[str] = set()  # services whose action is being verified
        self._lock = asyncio.Lock()
        self._sent: dict[str, int] = {}
        self._last_event: dict[str, str] = {}
        self._firing: list[Alert] = []
        self._recovery: dict[str, tuple[Incident, datetime, str]] = {}  # service -> its window
        self._after_effects: dict[tuple[str, str], AfterEffect] = {}
        self._handed_over: set[tuple[str, str]] = set()
        self._symptoms: dict[tuple[str, str], Incident] = {}  # (alert, service) -> its root
        self._waiting: dict[tuple[str, str], datetime] = {}  # a symptom waiting for its root
        self._idle_root: dict[tuple[str, str], datetime] = {}  # a symptom's root no longer active
        self._escalated_root: dict[str, Incident] = {}  # by service: its last escalated incident
        self.settings = AgentSettings(
            environment=config.environment,
            approval_required=config.approval_by_default,
            paused=False,
            updated_at=now(),
            updated_by=None,
        )

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        stored = await self._store.settings(self._cfg.environment)
        if stored is not None:
            self.settings = stored
        self._actions = await self._store.actions_since(self._now() - timedelta(hours=1))
        for incident in await self._store.recent(50):
            if incident.status is not Status.RESOLVED:  # left open by a restart of the agent
                events = incident.resolve(
                    Resolution.ESCALATED,
                    "The agent restarted while handling this incident: a person takes over.",
                    self._now(),
                )
                await self._emit(incident, events)

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("agent tick failed")
            await self._sleep(self._cfg.poll_seconds)

    async def stop(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def wait_idle(self) -> None:
        """Until every incident task has ended (tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    # ------------------------------------------------------------------ reading

    @property
    def model(self) -> str:
        """The configured model (Q-20): configuration, never a setting."""
        return self._decider.model

    def open_incidents(self) -> list[Incident]:
        return list(self._open.values())

    def incident(self, incident_id: str) -> Incident | None:
        return next((i for i in self._open.values() if i.incident_id == incident_id), None)

    def budget(self) -> dict[str, Any]:
        """What the feed shows (owner, 2026-11-24): actions left this hour and each cooldown."""
        now = self._now()
        recent = [a for a in self._actions if a.executed_at >= now - timedelta(hours=1)]
        cooldowns: dict[str, int] = {}
        for a in recent:
            left = (a.executed_at + guardrails.COOLDOWN - now).total_seconds()
            if left > 0:
                cooldowns[a.service] = max(cooldowns.get(a.service, 0), round(left))
        return {
            "actions_left_this_hour": max(0, guardrails.PER_HOUR - len(recent)),
            "cooldown_seconds_by_service": cooldowns,
        }

    # ------------------------------------------------------------------ the loop

    def _publish_budget(self) -> None:
        if self._metrics is None:
            return
        budget = self.budget()
        self._metrics.sre_agent_actions_left_this_hour.set(budget["actions_left_this_hour"])
        cooldowns = budget["cooldown_seconds_by_service"]
        for service in sorted(APPLICATION_SERVICES):
            self._metrics.sre_agent_cooldown_seconds.labels(service).set(cooldowns.get(service, 0))

    async def tick(self) -> None:
        self._publish_budget()
        alerts = await self._alerts.firing_alerts()
        self._firing = alerts
        now = self._now()
        keys = {(a.name, a.service) for a in alerts}
        self._escalated &= keys  # a cleared alert may open an incident again
        self._handed_over &= keys
        await self._watch_after_effects(alerts, now)
        await self._watch_symptoms(alerts, now)
        self._waiting = {k: t for k, t in self._waiting.items() if k in keys}
        by_service: dict[str, list[Alert]] = {}
        for alert in alerts:
            if alert.service in APPLICATION_SERVICES:  # never a data store
                by_service.setdefault(alert.service, []).append(alert)
        # The gateway last: an upstream's incident it may be a symptom of opens first.
        for service, group in sorted(by_service.items(), key=lambda kv: (kv[0] == GATEWAY, kv[0])):
            if service in self._open:
                continue
            group = [a for a in group if (a.name, service) not in self._after_effects]
            group = [a for a in group if (a.name, service) not in self._symptoms]
            if service == GATEWAY:
                group = [a for a in group if not await self._symptom_of_upstream(a, now)]
            window = self._recovery.get(service)
            if window is not None and now < window[1]:
                for alert in [a for a in group if a.name in AFTER_EFFECT_ALERTS]:
                    if (alert.name, service) not in self._handed_over:
                        await self._attach(window, alert, now)
                group = [a for a in group if (a.name, service) not in self._after_effects]
            if not group:
                continue
            lead = lead_alert(group)
            if (lead.name, service) in self._escalated:
                continue
            await self._open_incident(lead)

    # ------------------------------------------------------------------ symptoms

    async def _symptom_of_upstream(self, alert: Alert, now: datetime) -> bool:
        """True if this gateway alert is (or may soon be) a symptom of an upstream's incident:
        attached to it, or waiting for it to open. False: an incident of its own."""
        if (alert.name, alert.service) in self._handed_over:
            return False
        try:
            share, upstream = await self._collector.passed_on(alert.service)
        except Exception:  # Prometheus not answering: decide as before, with its own incident
            return False
        if share is None or share < PASSED_ON or upstream is None:
            return False  # the gateway's own errors
        root = self._open.get(upstream)
        window = self._recovery.get(upstream)
        if root is None and window is not None and now < window[1]:
            root = window[0]
        escalated = self._escalated_root.get(upstream)
        if root is None and escalated is not None and self._root_alert_firing(escalated):
            root = escalated  # declined: its cause is still there, so are its symptoms
        if root is not None:
            self._waiting.pop((alert.name, alert.service), None)
            self._symptoms[(alert.name, alert.service)] = root
            root.note(
                Kind.SYMPTOM,
                f"{alert.name} on {alert.service}: a symptom of {root.incident_id}."
                f" {round(100 * share)}% of the gateway's errors are {upstream}'s answers, passed"
                " on; no action.",
                now,
            )
            await self._emit(root, [])
            return True
        first = self._waiting.setdefault((alert.name, alert.service), now)
        return (now - first).total_seconds() < self._cfg.symptom_wait_seconds

    def _root_alert_firing(self, incident: Incident) -> bool:
        return any(
            (a.name, a.service) == (incident.alert_name, incident.service) for a in self._firing
        )

    async def _watch_symptoms(self, alerts: list[Alert], now: datetime) -> None:
        firing = {(a.name, a.service): a for a in alerts}
        for key, root in list(self._symptoms.items()):
            if key not in firing:
                del self._symptoms[key]
                self._idle_root.pop(key, None)
                root.note(Kind.SYMPTOM, f"{key[0]} on {key[1]} cleared with its cause.", now)
                await self._emit(root, [])
                continue
            if root.resolved_at is None or (
                root.resolution is Resolution.ESCALATED and self._root_alert_firing(root)
            ):
                self._idle_root.pop(key, None)
                continue  # its root is still active
            idle = self._idle_root.setdefault(key, now)
            if (now - idle).total_seconds() < self._cfg.symptom_grace_seconds:
                continue
            del self._symptoms[key]
            del self._idle_root[key]
            self._handed_over.add(key)
            root.note(
                Kind.SYMPTOM,
                f"{key[0]} on {key[1]} still fires after {root.incident_id} was resolved:"
                " handed to a person as an incident of its own.",
                now,
            )
            await self._emit(root, [])
            if key[1] not in self._open:
                await self._open_incident(firing[key])

    # ------------------------------------------------------------------ after-effects

    async def _attach(
        self, window: tuple[Incident, datetime, str], alert: Alert, now: datetime
    ) -> None:
        incident, ends, action = window
        key = (alert.name, alert.service)
        self._after_effects[key] = AfterEffect(incident, now, ends, [(now, alert.value)])
        level = f" ({alert.value:.0f} now)" if alert.value is not None else ""
        minutes = (ends - now).total_seconds() / 60
        incident.note(
            Kind.AFTER_EFFECT,
            f"{alert.name}{level} after the {action.replace('_deployment', '')}: an expected"
            f" after-effect, draining; no action. Watched for up to {minutes:.0f} min.",
            now,
        )
        await self._emit(incident, [])

    async def _watch_after_effects(self, alerts: list[Alert], now: datetime) -> None:
        for key, effect in list(self._after_effects.items()):
            alert = next((a for a in alerts if (a.name, a.service) == key), None)
            incident = effect.incident
            if alert is None:
                seconds = round((now - effect.since).total_seconds())
                incident.note(
                    Kind.AFTER_EFFECT,
                    f"{key[0]} cleared after {seconds} s: the after-effect drained.",
                    now,
                )
                del self._after_effects[key]
                await self._emit(incident, [])
                continue
            effect.samples.append((now, alert.value))
            why = None
            if now >= effect.ends:
                why = "still firing when the recovery window ended"
            else:
                stalled = effect.stalled(now, self._cfg.drain_check_seconds)
                if stalled is not None:
                    why = f"stopped draining ({stalled[0]:.0f} to {stalled[1]:.0f} in a minute)"
            if why is None:
                continue
            del self._after_effects[key]
            self._handed_over.add(key)
            incident.note(Kind.AFTER_EFFECT, f"{key[0]} {why}: handed to a person.", now)
            await self._emit(incident, [])
            if alert.service not in self._open:
                await self._open_incident(alert)

    async def _open_incident(self, lead: Alert) -> None:
        number = await self._store.next_incident_number()
        incident, events = Incident.open(
            number,
            lead.name,
            lead.service,
            lead.severity,
            lead.summary,
            self._now(),
            fired_at=lead.fired_at,
        )
        self._open[lead.service] = incident
        await self._emit(incident, events)
        task = asyncio.create_task(self._handle(incident, lead))
        self._tasks[incident.incident_id] = task

    async def _handle(self, incident: Incident, lead: Alert) -> None:
        try:
            await self._rounds(incident, lead)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("incident failed", extra={"incident_id": incident.incident_id})
            await self._resolve(
                incident,
                Resolution.ESCALATED,
                f"The agent hit an error ({exc}): a person takes over.",
            )
        finally:
            self._open.pop(incident.service, None)
            self._tasks.pop(incident.incident_id, None)
            self._acting.discard(incident.service)
            if incident.resolution is Resolution.ESCALATED and self._is_firing(lead):
                self._escalated.add((lead.name, lead.service))

    async def _rounds(self, incident: Incident, lead: Alert) -> None:
        for _ in range(self._cfg.max_rounds):
            if not await self._still_firing(lead):
                recovered = await self._wait_clear(lead, 0)
                if recovered is not None:
                    await self._recovered(incident, recovered)
                    return
            collected = await self._collect(incident, lead)
            decision = await self._decider.decide(lead, collected.evidence, collected.signals)
            await self._diagnosed(incident, decision)
            done = await self._act(incident, lead, decision, collected)
            if done:
                return
        await self._resolve(
            incident,
            Resolution.ESCALATED,
            f"Still firing after {self._cfg.max_rounds} decisions: a person takes over.",
        )

    async def _collect(self, incident: Incident, lead: Alert) -> Collected:
        others = [a for a in self._firing if a.service == lead.service]
        collected = await self._collector.collect(lead, others, incident, self._actions)
        incident.evidence_summary = collected.evidence.summary()
        incident.note(Kind.EVIDENCE_COLLECTED, incident.evidence_summary, self._now())
        await self._emit(incident, [])
        return collected

    async def _diagnosed(self, incident: Incident, decision: Decision) -> None:
        p = decision.proposal
        prefix = (
            f"Rule-based (LLM unavailable: {decision.fallback_reason}). "
            if decision.by_rules
            else ""
        )
        incident.note(Kind.DIAGNOSIS, prefix + p.explanation, self._now())
        await self._emit(incident, [])

    def _situation(self, incident: Incident, collected: Collected) -> Situation:
        now = self._now()
        hour_ago = now - timedelta(hours=1)
        last: dict[str, datetime] = {}
        for a in self._actions:
            if a.service not in last or a.executed_at > last[a.service]:
                last[a.service] = a.executed_at
        service = incident.service
        return Situation(
            now=now,
            paused=self.settings.paused,
            executed_this_incident=len(incident.actions_taken),
            executed_last_hour=sum(1 for a in self._actions if a.executed_at >= hour_ago),
            last_action_at=last,
            acting_on=frozenset(self._acting),
            incident_service=service,
            replicas={service: collected.deployment.desired} if collected.deployment else {},
            has_previous_revision={
                service: bool(collected.revision and collected.revision.rollback_to)
            },
            revision_new={service: bool(collected.revision and collected.revision.new)},
        )

    async def _act(
        self, incident: Incident, lead: Alert, decision: Decision, collected: Collected
    ) -> bool:
        """One round's proposal to its end. True when the incident is over."""
        p = decision.proposal
        verdict = guardrails.check(p, self._situation(incident, collected))
        now = self._now()
        if not verdict.allowed:
            _, events = incident.decline_proposal(p, verdict.explanation, verdict.reason, now)
            self._count_decline("guardrail", verdict.guardrail or "?")
            await self._emit(incident, events)
            await self._resolve(
                incident, Resolution.ESCALATED, f"{verdict.reason} A person takes over."
            )
            return True

        parameters: dict[str, Any] = {}
        if p.tool == Tool.SCALE:
            parameters["replicas"] = p.replicas
        if p.tool == Tool.ROLLBACK and collected.revision and collected.revision.rollback_to:
            parameters["to_revision"] = collected.revision.rollback_to
        needs_approval = self.settings.approval_required and p.tool != Tool.NOTHING
        r, events = incident.propose(p, verdict.explanation, parameters, needs_approval, now)
        await self._emit(incident, events)

        if p.tool == Tool.NOTHING:
            events = incident.executed(r.remediation_id, True, "No action taken.", self._now())
            await self._emit(incident, events)
            if decision.escalate:
                await self._resolve(
                    incident, Resolution.ESCALATED, "No safe action: a person takes over."
                )
                return True
            recovered = await self._wait_clear(lead, self._cfg.verify_seconds[Tool.NOTHING])
            if recovered is not None:
                await self._recovered(incident, recovered)
                return True
            incident.note(Kind.VERIFIED, "Still firing: deciding again.", self._now())
            await self._emit(incident, [])
            return False

        if needs_approval and not await self._approved(incident, r):
            await self._resolve(
                incident, Resolution.ESCALATED, "Not approved: a person takes over."
            )
            return True

        async with self._lock:  # check and act as one step: two incidents never race
            verdict = guardrails.check(p, self._situation(incident, collected))
            if not verdict.allowed:
                events = incident.executed(
                    r.remediation_id, False, f"Not run: {verdict.reason}", self._now()
                )
                await self._emit(incident, events)
                await self._resolve(
                    incident, Resolution.ESCALATED, f"{verdict.reason} A person takes over."
                )
                return True
            self._acting.add(incident.service)
            first_ran = collected.revision.first_ran_at if collected.revision else None
            succeeded, details, git = await self._execute(incident, r, first_ran)
            executed_at = self._now()
            if succeeded:
                record = ActionRecord(incident.service, executed_at, incident.incident_id, r.action)
                self._actions.append(record)
                with contextlib.suppress(Exception):
                    await self._store.record_action(record)
        outcome = "SUCCEEDED" if succeeded else "FAILED"
        if self._metrics is not None:
            self._metrics.sre_agent_actions_total.labels(r.action, incident.service, outcome).inc()
        # One line per action: Grafana's Incident view marks each on its panels (an annotation).
        logger.info(
            "action executed",
            extra={
                "incident_id": incident.incident_id,
                "action": r.action,
                "target_service": incident.service,
                "outcome": outcome,
                "details": details,
            },
        )
        events = incident.executed(r.remediation_id, succeeded, details, executed_at, git)
        await self._emit(incident, events)
        if not succeeded:
            await self._resolve(
                incident, Resolution.ESCALATED, "The action failed: a person takes over."
            )
            return True
        recovered = await self._wait_clear(lead, self._cfg.verify_seconds[r.action])
        self._acting.discard(incident.service)
        if recovered is not None:
            await self._recovered(incident, recovered)
            return True
        waited = self._cfg.verify_seconds[r.action]
        incident.note(
            Kind.VERIFIED, f"{lead.name} still firing {waited:.0f} s after the action.", self._now()
        )
        await self._emit(incident, [])
        return False

    async def _execute(
        self, incident: Incident, r: Remediation, first_ran: datetime | None = None
    ) -> tuple[bool, str, dict[str, Any] | None]:
        service = r.target_service or incident.service
        try:
            if r.action == Tool.ROLLBACK:
                before = await self._gitops.sync_state(service)
                details = await self._platform.rollback(service, int(r.parameters["to_revision"]))
                git = await self._gitops.reconcile(service, before, incident.incident_id, first_ran)
                return True, details, git
            if r.action == Tool.SCALE:
                return (
                    True,
                    await self._platform.scale(service, int(r.parameters["replicas"])),
                    None,
                )
            if r.action == Tool.RESTART:
                return True, await self._platform.restart(service), None
        except (PlatformError, KeyError, ValueError) as exc:
            return False, f"{r.action} on {service} failed: {exc}", None
        return False, f"{r.action} is not an action the agent can run.", None

    async def _approved(self, incident: Incident, r: Remediation) -> bool:
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._decisions[r.remediation_id] = future
        try:
            return await asyncio.wait_for(
                asyncio.shield(future), self._cfg.approval_timeout_seconds
            )
        except TimeoutError:
            minutes = self._cfg.approval_timeout_seconds / 60
            events = incident.decline(
                r.remediation_id, f"No decision within {minutes:.0f} minutes.", self._now()
            )
            self._count_decline("guardrail", "approval timed out")
            await self._emit(incident, events)
            return False
        finally:
            self._decisions.pop(r.remediation_id, None)

    async def decide(
        self, incident_id: str, remediation_id: str, approve: bool, by: str, comment: str | None
    ) -> Remediation:
        """A-15. Raises NotPendingError (409) unless that remediation is waiting for a decision."""
        incident = self.incident(incident_id)
        future = self._decisions.get(remediation_id)
        if incident is None or future is None or future.done():
            raise NotPendingError("Nothing is waiting for a decision there.")
        now = self._now()
        if approve:
            events = incident.approve(remediation_id, by, comment, now)
        else:
            reason = comment or "No reason given."
            events = incident.decline(remediation_id, reason, now, admin=by)
            self._count_decline("admin", "admin")
        await self._emit(incident, events)
        future.set_result(approve)
        return incident.find(remediation_id)

    async def update_settings(
        self, approval_required: bool | None, paused: bool | None, by: str
    ) -> AgentSettings:
        """A-14. The model and the tools are not settings."""
        s = self.settings
        self.settings = AgentSettings(
            environment=s.environment,
            approval_required=s.approval_required
            if approval_required is None
            else approval_required,
            paused=s.paused if paused is None else paused,
            updated_at=self._now(),
            updated_by=by,
        )
        await self._store.save_settings(self.settings)
        return self.settings

    # ------------------------------------------------------------------ verifying

    def _is_firing(self, lead: Alert) -> bool:
        return any(a.name == lead.name and a.service == lead.service for a in self._firing)

    async def _still_firing(self, lead: Alert) -> bool:
        try:
            self._firing = await self._alerts.firing_alerts()
        except Exception:
            logger.warning("alerts unavailable while verifying: assumed still firing")
            return True
        return self._is_firing(lead)

    async def _wait_clear(self, lead: Alert, window: float) -> datetime | None:
        """When the alert stopped firing, if it then stayed clear for the hold time within the
        window (a clear that began inside the window may finish its hold after it)."""
        deadline = self._now() + timedelta(seconds=window)
        clear_since: datetime | None = None
        while True:
            firing = await self._still_firing(lead)
            now = self._now()
            if firing:
                clear_since = None
                if now >= deadline:
                    return None
            else:
                clear_since = clear_since or now
                if (now - clear_since).total_seconds() >= self._cfg.hold_seconds:
                    return clear_since
            await self._sleep(self._cfg.poll_seconds)

    async def _recovered(self, incident: Incident, cleared_at: datetime) -> None:
        incident.recovered_at = cleared_at
        acted = bool(incident.actions_taken)
        resolution = Resolution.RECOVERED if acted else Resolution.AUTO_CLEARED
        seconds = round((cleared_at - incident.opened_at).total_seconds())
        message = (
            f"Recovered: {incident.alert_name} cleared {seconds} s after the incident opened"
            f" and stayed clear for {self._cfg.hold_seconds:.0f} s."
            if acted
            else f"Cleared on its own and stayed clear for {self._cfg.hold_seconds:.0f} s."
        )
        if self._metrics is not None and acted:
            self._metrics.sre_agent_time_to_recover_seconds.observe(max(0, seconds))
        if acted:
            ends = self._now() + timedelta(seconds=self._cfg.after_effect_seconds)
            self._recovery[incident.service] = (incident, ends, incident.actions_taken[-1])
        await self._resolve(incident, resolution, message)

    async def _resolve(self, incident: Incident, resolution: Resolution, message: str) -> None:
        events = incident.resolve(resolution, message, self._now())
        if resolution is Resolution.ESCALATED:
            self._escalated_root[incident.service] = incident
        if events and self._metrics is not None:
            self._metrics.sre_agent_incidents_total.labels(
                incident.alert_name, resolution.value
            ).inc()
        await self._emit(incident, events)

    def _count_decline(self, by: str, reason: str) -> None:
        if self._metrics is not None:
            self._metrics.sre_agent_declined_total.labels(by, reason).inc()

    # ------------------------------------------------------------------ telling everyone

    async def _emit(self, incident: Incident, events: list[Event]) -> None:
        """Store the incident, publish its events, push its new timeline entries to the feed."""
        iid = incident.incident_id
        for event_type, data in events:
            envelope = build_event(
                event_type,
                data,
                producer="sre-agent",
                environment=self._cfg.environment,
                correlation_id=iid,
                causation_id=self._last_event.get(iid),
                occurred_at=self._now(),
            )
            self._last_event[iid] = envelope.event_id
            if self._sink is not None:
                try:
                    await self._sink.publish(envelope)
                except Exception:  # the feed and the store still show it
                    logger.exception("could not publish", extra={"event_type": event_type})
        try:
            await self._store.save(incident)
        except Exception:
            logger.exception("could not store incident", extra={"incident_id": iid})
        types = [t for t, _ in events]
        if "IncidentOpened" in types:
            self.feed.publish("incident_opened", incident.to_summary())
        for entry in incident.timeline[self._sent.get(iid, 0) :]:
            self.feed.publish("incident_update", {"incident_id": iid, "entry": entry.to_api()})
        self._sent[iid] = len(incident.timeline)
        pending = incident.pending
        if "RemediationProposed" in types and pending is not None and pending.requires_approval:
            self.feed.publish(
                "approval_required", {"incident_id": iid, "remediation": pending.to_api()}
            )
        if "IncidentResolved" in types:
            self.feed.publish("incident_resolved", incident.to_summary())
