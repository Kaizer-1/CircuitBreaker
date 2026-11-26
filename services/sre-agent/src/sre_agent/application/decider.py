"""Who chooses the tool: the model, or the rules when the model gives no usable answer (ADR-0021).

An alert labelled remediable=false never reaches either: it gets a fixed explanation and an
escalation (declines.py), so the most important "no" never depends on a model's wording.
"""

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sre_agent.domain import fallback
from sre_agent.domain.actions import Proposal, Tool
from sre_agent.domain.declines import decline_reason
from sre_agent.domain.evidence import Evidence
from sre_agent.domain.signals import Signals
from sre_agent.ports.llm import LLMClient, LLMUnavailableError
from sre_agent.ports.observability import Alert

logger = logging.getLogger("decider")

DEADLINE_SECONDS = 25.0  # every attempt together: mitigation should start within 30 s
CALLS_PER_HOUR = 60
FIXED = "fixed rule (alert is information only)"


@dataclass(frozen=True)
class Decision:
    proposal: Proposal
    fallback_reason: str | None  # why the rules chose; None when the model did
    escalate: bool  # a do_nothing that hands over to a person at once

    @property
    def by_rules(self) -> bool:
        return self.fallback_reason is not None


class Decider:
    def __init__(
        self,
        llm: LLMClient | None,
        now: Callable[[], datetime],
        *,
        calls_per_hour: int = CALLS_PER_HOUR,
        deadline_seconds: float = DEADLINE_SECONDS,
    ) -> None:
        self._llm = llm
        self._now = now
        self._calls: deque[datetime] = deque()
        self._per_hour = calls_per_hour
        self._deadline = deadline_seconds

    @property
    def model(self) -> str:
        return self._llm.model if self._llm is not None else "none (rules only)"

    def _budget_left(self) -> bool:
        hour_ago = self._now() - timedelta(hours=1)
        while self._calls and self._calls[0] < hour_ago:
            self._calls.popleft()
        return len(self._calls) < self._per_hour

    async def decide(self, alert: Alert, evidence: Evidence, signals: Signals) -> Decision:
        if not alert.remediable:
            text = decline_reason(signals)
            return Decision(Proposal(Tool.NOTHING.value, None, None, text, FIXED), None, True)
        reason: str
        if self._llm is None:
            reason = "no model is configured"
        elif not self._budget_left():
            reason = f"{self._per_hour} model calls in the last hour already"
        else:
            self._calls.append(self._now())
            try:
                choice = await self._llm.choose_action(evidence, self._deadline)
                return Decision(choice.proposal, None, False)
            except LLMUnavailableError as exc:
                reason = str(exc)
                logger.warning("model unavailable, rules decide", extra={"reason": reason})
        proposal = fallback.decide(signals)
        return Decision(proposal, reason, proposal.tool == Tool.NOTHING)
