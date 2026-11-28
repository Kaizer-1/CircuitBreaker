"""The model, behind one small interface (SRE agent design section 4): an Anti-Corruption Layer,
so the rest of the agent never sees OpenRouter's request or response format."""

from dataclasses import dataclass
from typing import Protocol

from sre_agent.domain.actions import Proposal
from sre_agent.domain.evidence import Evidence


class LLMUnavailableError(Exception):
    """No usable tool call, so the rules decide (ADR-0021). This class itself means the model
    never answered: OpenRouter unreachable, too slow, refusing, out of credit. Its `label` and the
    reason are shown on the feed."""

    label = "LLM unavailable"


class MalformedAnswerError(LLMUnavailableError):
    """The model did answer, but not with exactly one valid tool call: text only, several calls,
    an unknown tool, arguments that are not JSON, a cut-off answer, or (staging, 2026-11-27) a
    provider that reported a tool call and sent none. Labelled as such, never "unavailable"."""

    label = "malformed model answer"

    def __init__(self, reason: str, provider: str | None = None) -> None:
        super().__init__(reason)
        self.provider = provider  # who answered, so a second try can route around it


@dataclass(frozen=True)
class Choice:
    proposal: Proposal
    model: str
    seconds: float
    tokens: int
    cost_dollars: float | None = None  # what OpenRouter charged for this call
    attempts: int = 1  # 2 when the first answer could not be used (or failed fast)


class LLMClient(Protocol):
    @property
    def model(self) -> str: ...

    async def choose_action(self, evidence: Evidence, deadline_seconds: float) -> Choice:
        """One tool call, or LLMUnavailableError (no answer: timeout, refusal...) or its
        subclass MalformedAnswerError (an answer without one valid tool call)."""
        ...
