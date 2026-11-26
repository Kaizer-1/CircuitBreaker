"""The rule-based decision when the model is unavailable (ADR-0021, an owner's exception amending
ADR-0009's "do_nothing and escalate"): OpenRouter unreachable, too slow, refusing, answering without
a valid tool call, or over its call budget. Labelled "rule-based (LLM unavailable)" on the feed. Its
proposals go through the same guardrails as the model's.

The rules read the error TYPE, the running revision (new or known-good) and the load, never "a
deploy happened" alone: right after the agent rolls back a bad release (itself a new rollout), an
overload must still be scaled, and only errors from a new release are rolled back. Anything the
rules do not recognise is left alone and escalated: no rule guesses.
"""

from sre_agent.domain.actions import (
    MAX_REPLICAS,
    MAX_STEP,
    NORMAL_REPLICAS,
    SCALABLE,
    Proposal,
    Tool,
)
from sre_agent.domain.signals import Signals

SOURCE = "rule-based (LLM unavailable)"

ERROR_ALERTS = {"OrderServiceHighErrorRate", "GatewayHighErrorRate", "PortfolioHighErrorRate"}
PASSED_ON = 0.5  # of a gateway's 5xx answered by an upstream: the upstream's problem


def _scale_up(s: Signals, why: str) -> Proposal:
    if s.service not in SCALABLE or s.replicas >= MAX_REPLICAS:
        return _nothing(f"{why} More replicas are not possible here, so a person must look.")
    target = min(MAX_REPLICAS, s.replicas + MAX_STEP)
    text = f"{why} Scaling {s.service} from {s.replicas} to {target} replicas to share the load."
    return Proposal(Tool.SCALE.value, s.service, target, text, SOURCE)


def _rollback(s: Signals, why: str) -> Proposal:
    text = f"{why} Rolling {s.service} back to the revision before it."
    return Proposal(Tool.ROLLBACK.value, s.service, None, text, SOURCE)


def _restart(s: Signals, why: str) -> Proposal:
    text = f"{why} Restarting {s.service}: a fresh process starts with clean memory."
    return Proposal(Tool.RESTART.value, s.service, None, text, SOURCE)


def _nothing(why: str) -> Proposal:
    return Proposal(Tool.NOTHING.value, None, None, why, SOURCE)


def _ago(minutes: float | None) -> str:
    if minutes is None:
        return "recently"
    if minutes < 1:
        return "less than a minute ago"
    return "1 minute ago" if round(minutes) == 1 else f"{minutes:.0f} minutes ago"


def decide(s: Signals) -> Proposal:
    deployed = _ago(s.revision_minutes)
    if s.alert == "MarketDataMemoryGrowth" or (s.memory_rising and s.memory_of_limit > 0.7):
        if s.new_revision:
            return _rollback(s, f"Memory climbs since a new release ({deployed}).")
        return _restart(
            s,
            f"{s.service}'s memory is at {100 * s.memory_of_limit:.0f}% of its limit and still"
            " climbing, with no new release and normal traffic: a leak in the running process.",
        )

    if s.alert in ERROR_ALERTS or s.alert == "OrderServiceHighLatency":
        if s.passed_on_share >= PASSED_ON:
            source = s.upstream or "the services behind it"
            return _nothing(
                f"Most of {s.service}'s errors were answered by {source} and only passed on:"
                f" the problem is in {source}, which has its own incident. Acting on {s.service}"
                " would not help. Escalating."
            )
        if s.breaker_open or s.main_error == "DEPENDENCY_UNAVAILABLE":
            return _nothing(
                f"{s.service}'s errors come from a dependency that is failing (its circuit breaker"
                " is open), not from this service: acting on it would not help. Escalating."
            )
        if s.main_error == "OVERLOADED" or (
            s.alert == "OrderServiceHighLatency" and s.load_far_above_normal
        ):
            ratio = s.request_rate / s.normal_rate if s.normal_rate else 0
            more = f"{ratio:.0f}x its normal traffic" if ratio else "far more traffic than usual"
            return _scale_up(
                s,
                f"{s.service} gets {more} and turns requests away as overloaded (or answers"
                " slowly); this is load, not a bug.",
            )
        if s.main_error == "INTERNAL_ERROR" and s.new_revision:
            return _rollback(
                s, f"{s.service} fails with internal errors since a new release ({deployed})."
            )
        if s.main_error == "INTERNAL_ERROR":
            return _nothing(
                f"{s.service} fails with internal errors, but its running revision is not new"
                " (or is the known-good one): rolling back would not help. Escalating."
            )
        return _nothing(f"{s.service} is slow with no clear cause in the evidence. Escalating.")

    if s.alert == "PodCrashLooping":
        if s.new_revision:
            return _rollback(s, f"{s.service} keeps crashing since a new release ({deployed}).")
        return _nothing(f"{s.service} keeps crashing with no new release. Escalating.")

    if s.alert == "OrderServiceOverProvisioned":
        if s.replicas > NORMAL_REPLICAS and s.service in SCALABLE:
            target = max(NORMAL_REPLICAS, s.replicas - MAX_STEP)
            text = (
                f"{s.service} uses little CPU with {s.replicas} replicas: the spike is over."
                f" Scaling back to {target}."
            )
            return Proposal(Tool.SCALE.value, s.service, target, text, SOURCE)
        return _nothing(f"{s.service} is already at its normal size.")

    return _nothing(f"No rule fits {s.alert} on {s.service}. Escalating to a person.")
