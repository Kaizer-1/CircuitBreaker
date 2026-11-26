"""The model, behind one small interface (SRE agent design section 4): an Anti-Corruption Layer,
so the rest of the agent never sees OpenRouter's request or response format."""

from dataclasses import dataclass
from typing import Protocol

from sre_agent.domain.actions import Proposal
from sre_agent.domain.evidence import Evidence


class LLMUnavailableError(Exception):
    """No usable tool call: the reason is shown on the feed (ADR-0021)."""


@dataclass(frozen=True)
class Choice:
    proposal: Proposal
    model: str
    seconds: float
    tokens: int
    cost_dollars: float | None = None  # what OpenRouter charged for this call


class LLMClient(Protocol):
    @property
    def model(self) -> str: ...

    async def choose_action(self, evidence: Evidence, deadline_seconds: float) -> Choice:
        """One tool call, or LLMUnavailableError (timeout, refusal, no tool call, not JSON...)."""
        ...
