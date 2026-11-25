"""Running chaos experiments: A-10 start, A-11 stop, A-20 reset, Q-21/Q-22/Q-23, and expiry.

Experiments live in memory (event catalog: local events `ChaosExperimentStarted` / `Ended`, also
logged). Undoing never trusts that memory alone: stop and reset look at what is actually running
(the order-service Deployment, the three hooks), so a reset after chaos-service itself restarted
still leaves nothing behind.

The agent's incidents (incidents.v1, read here; chaos-service never sends anything to the agent)
fill in each experiment's timeline: time to detect (IncidentOpened - start), its actions, and time
to recover (IncidentResolved - start). An experiment is REMEDIATED when the incident on its primary
service is resolved RECOVERED; an ESCALATED one stays RUNNING until a person stops or resets it.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from chaos_service.domain.scenarios import (
    SCENARIOS,
    Experiment,
    Scenario,
    ScenarioId,
    Status,
    conflicting,
)
from chaos_service.ports.targets import FaultHooks, OrderServiceDeployment, TargetUnavailableError
from chaos_service.runtime.events import EventEnvelope, parse_timestamp

logger = logging.getLogger("chaos")

NORMAL_ORDER_SERVICE_REPLICAS = 2  # the chart's default; a reset falls back to it


class AlreadyRunningError(Exception):
    def __init__(self, running: Experiment) -> None:
        super().__init__(running.experiment_id)
        self.running = running


class NotRunningError(Exception):
    def __init__(self, experiment: Experiment) -> None:
        super().__init__(experiment.experiment_id)
        self.experiment = experiment


@dataclass(frozen=True)
class Reset:
    at: datetime
    undone: list[str]
    failed: list[str]


class ChaosLab:
    def __init__(
        self,
        deployment: OrderServiceDeployment,
        hooks: FaultHooks,
        *,
        replicas_known: bool = True,  # False outside Kubernetes: no replicas to put back
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], str] = lambda: str(uuid4()),
        on_started: Callable[[str], None] = lambda scenario: None,
        on_ended: Callable[[Experiment], None] = lambda experiment: None,
        on_reset: Callable[[], None] = lambda: None,
    ) -> None:
        self._deployment = deployment
        self._hooks = hooks
        self._replicas_known = replicas_known
        self._clock = clock
        self._new_id = new_id
        self._on_started = on_started
        self._on_ended = on_ended
        self._on_reset = on_reset
        self._lock = asyncio.Lock()
        self.experiments: list[Experiment] = []  # newest last
        self.resets: list[Reset] = []
        self._expiries: dict[str, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------ queries

    @staticmethod
    def scenarios() -> list[Scenario]:
        return list(SCENARIOS.values())

    def get(self, experiment_id: str) -> Experiment | None:
        return next((e for e in self.experiments if e.experiment_id == experiment_id), None)

    def running(self) -> list[Experiment]:
        return [e for e in self.experiments if e.running]

    # ------------------------------------------------------------------ A-10 start

    async def start(
        self,
        scenario_id: ScenarioId,
        duration_seconds: int | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> Experiment:
        scenario = SCENARIOS[scenario_id]
        async with self._lock:
            if (other := conflicting(scenario, self.running())) is not None:
                raise AlreadyRunningError(other)
            params = scenario.defaults | {k: v for k, v in (parameters or {}).items() if v}
            duration = duration_seconds or scenario.default_duration_seconds
            experiment = Experiment(self._new_id(), scenario, params, self._clock(), duration)
            message = await self._inject(experiment)  # raises TargetUnavailableError: no record
            experiment.log(experiment.started_at, f"Started by the presenter. {message}")
            self.experiments.append(experiment)
            self._expiries[experiment.experiment_id] = asyncio.create_task(
                self._expire_later(experiment)
            )
        self._on_started(scenario_id.value)
        logger.info(
            "ChaosExperimentStarted",
            extra={
                "experiment_id": experiment.experiment_id,
                "scenario": scenario_id.value,
                "parameters": params,
                "duration_seconds": duration,
            },
        )
        return experiment

    async def _inject(self, e: Experiment) -> str:
        p = e.parameters
        match e.scenario.scenario:
            case ScenarioId.BAD_DEPLOY:
                revision = await self._deployment.deploy_bad_release(float(p["error_rate"]))
                number = f" (revision {revision})" if revision else ""
                return f"Deployed order-service release 1.4.1{number}."
            case ScenarioId.MEMORY_LEAK:
                await self._hooks.memory_leak_on(int(p["rate_mb_per_minute"]))
                return f"market-data-service leaks {p['rate_mb_per_minute']} MB a minute."
            case ScenarioId.TRAFFIC_SPIKE:
                e.replicas_before = await self._deployment.replicas()
                await self._hooks.panic_sell_on(int(p["requests_per_second"]), e.duration_seconds)
                return (
                    f"The bots panic: {p['requests_per_second']} requests a second, mostly market"
                    f" sells, at order-service ({e.replicas_before} replicas)."
                )
            case ScenarioId.PORTFOLIO_DB_DEGRADED:
                await self._hooks.database_degraded_on(int(p["delay_ms"]))
                return f"portfolio-service's database answers {p['delay_ms']} ms slower."

    # ------------------------------------------------------------ A-11 stop, expiry, A-20 reset

    async def stop(self, experiment_id: str) -> Experiment | None:
        async with self._lock:
            experiment = self.get(experiment_id)
            if experiment is None:
                return None
            if not experiment.running:
                raise NotRunningError(experiment)
            undone = await self._undo(experiment)
            self._end(experiment, Status.STOPPED, f"Stopped by the presenter. {undone}")
        return experiment

    async def _expire_later(self, experiment: Experiment) -> None:
        remaining = (experiment.ends_at - self._clock()).total_seconds()
        await asyncio.sleep(max(0.0, remaining))
        async with self._lock:
            if experiment.running:
                undone = await self._undo(experiment)
                self._end(experiment, Status.EXPIRED, f"Time is up. {undone}")

    async def reset(self) -> Reset:
        """End every experiment and undo every fault, from what is actually running (A-20).

        Each step is tried even if another fails (a target that is down is reported, not a reason
        to leave the rest broken)."""
        async with self._lock:
            undone: list[str] = []
            failed: list[str] = []

            async def step(name: str, action: Callable[[], Awaitable[str | None]]) -> None:
                try:
                    if (done := await action()) is not None:
                        undone.append(done)
                except TargetUnavailableError as exc:
                    failed.append(f"{name}: {exc}")

            async def bad_release() -> str | None:
                removed = await self._deployment.remove_bad_release()
                return "order-service's bad release removed" if removed else None

            async def leak() -> str | None:
                cleared = await self._hooks.memory_leak_off()
                return "market-data-service's leak cleared" if cleared else None

            async def database() -> str | None:
                cleared = await self._hooks.database_degraded_off()
                return "portfolio-service's database back to normal" if cleared else None

            async def panic() -> str | None:
                stopped = await self._hooks.panic_sell_off()
                return "the panic sell stopped" if stopped else None

            async def replicas() -> str | None:
                before = [e.replicas_before for e in self.experiments if e.replicas_before]
                normal = before[-1] if before else NORMAL_ORDER_SERVICE_REPLICAS
                now = await self._deployment.replicas()
                if now == normal:
                    return None
                await self._deployment.scale(normal)
                return f"order-service back to {normal} replicas (was {now})"

            await step("order-service", bad_release)
            await step("market-data-service", leak)
            await step("portfolio-service", database)
            await step("bot-traders", panic)
            if self._replicas_known:
                await step("order-service replicas", replicas)
            summary = "; ".join(undone) if undone else "nothing was left to undo"
            for experiment in self.running():
                self._end(experiment, Status.STOPPED, f"Presenter reset: {summary}.")
            reset = Reset(self._clock(), undone, failed)
            self.resets.append(reset)
        self._on_reset()
        logger.info("chaos reset", extra={"undone": undone, "failed": failed})
        return reset

    async def _undo(self, e: Experiment) -> str:
        """Undo this experiment's fault, if it is still there (the agent may have fixed it)."""
        match e.scenario.scenario:
            case ScenarioId.BAD_DEPLOY:
                done = await self._deployment.remove_bad_release()
                return "Bad release removed." if done else "The bad release was already gone."
            case ScenarioId.MEMORY_LEAK:
                done = await self._hooks.memory_leak_off()
                return "Leak cleared." if done else "The leak was already gone (restarted)."
            case ScenarioId.TRAFFIC_SPIKE:
                done = await self._hooks.panic_sell_off()
                return "Panic sell stopped." if done else "The panic sell had ended."
            case ScenarioId.PORTFOLIO_DB_DEGRADED:
                await self._hooks.database_degraded_off()
                return "portfolio-service's database is back to normal."

    def _end(self, experiment: Experiment, status: Status, message: str) -> None:
        experiment.end(status, self._clock(), message)
        expiry = self._expiries.pop(experiment.experiment_id, None)
        if expiry is not None and expiry is not asyncio.current_task():
            expiry.cancel()
        self._on_ended(experiment)
        logger.info(
            "ChaosExperimentEnded",
            extra={
                "experiment_id": experiment.experiment_id,
                "scenario": experiment.scenario.scenario.value,
                "outcome": status.value,
                "time_to_detect_seconds": experiment.time_to_detect_seconds,
                "time_to_recover_seconds": experiment.time_to_recover_seconds,
            },
        )

    # ------------------------------------------------------------------ the agent's incidents

    async def on_incident_event(self, envelope: EventEnvelope) -> None:
        """Fill in the timelines from incidents.v1 (never the other way round)."""
        d = envelope.data
        at = parse_timestamp(envelope.occurred_at)
        async with self._lock:
            match envelope.event_type:
                case "IncidentOpened":
                    self._opened(d, at)
                case "RemediationProposed" | "RemediationExecuted" | "RemediationDeclined":
                    self._remediation(envelope.event_type, d, at)
                case "IncidentResolved":
                    await self._resolved(d, at)

    def _experiment_for(self, incident_id: str) -> Experiment | None:
        return next((e for e in self.experiments if e.incident_id == incident_id), None)

    def _opened(self, d: dict[str, Any], at: datetime) -> None:
        service, opened_at = d["service"], parse_timestamp(d["opened_at"])
        for e in self.running():
            if service in e.scenario.affects and opened_at >= e.started_at:
                text = f"Agent opened {d['incident_id']} ({d['alert_name']} on {service})."
                if e.incident_id is None and service == e.scenario.primary:
                    e.incident_id = d["incident_id"]
                    e.time_to_detect_seconds = round((opened_at - e.started_at).total_seconds())
                    text += f" Detected after {e.time_to_detect_seconds} s."
                e.log(at, text)
                return

    def _remediation(self, kind: str, d: dict[str, Any], at: datetime) -> None:
        experiment = self._experiment_for(d["incident_id"])
        if experiment is None:
            return
        if kind == "RemediationProposed":
            experiment.log(at, f"Agent proposed {d['action']} on {d['target_service']}.")
        elif kind == "RemediationDeclined":
            experiment.log(at, f"Declined by {d['declined_by']}: {d['reason']}")
        else:
            experiment.agent_actions.append(d["action"])
            experiment.log(at, f"Agent ran {d['action']} ({d['outcome']}).")

    async def _resolved(self, d: dict[str, Any], at: datetime) -> None:
        experiment = self._experiment_for(d["incident_id"])
        if experiment is None:
            return
        resolved_at = parse_timestamp(d["resolved_at"])
        experiment.log(at, f"Incident {d['incident_id']} resolved: {d['resolution']}.")
        if d["resolution"] == "RECOVERED" and experiment.running:
            seconds = (resolved_at - experiment.started_at).total_seconds()
            experiment.time_to_recover_seconds = round(seconds)
            undone = await self._undo(experiment)
            self._end(experiment, Status.REMEDIATED, f"Remediated by the agent. {undone}")
