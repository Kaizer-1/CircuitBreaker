"""Alerts the agent must not act on (`remediable=false`; SRE agent design, "Alerts the agent must
NOT act on"): it opens an incident, explains and escalates, without asking the model for an action.
The explanation is fixed text, so the most important "no" is never left to a model's wording."""

from sre_agent.domain.signals import Signals


def decline_reason(s: Signals) -> str:
    if s.alert == "OrderServiceCircuitOpen":
        cause = (
            " The slow part is portfolio-service's database (Postgres): its transactions take far"
            " longer than normal. A database is a data store, which the agent may never touch."
            if s.database_slow
            else " The cause is in portfolio-service or behind it."
        )
        return (
            "order-service's circuit breaker to portfolio-service is open: portfolio-service is"
            " failing or too slow, and order-service is doing the right thing by answering at once"
            " instead of waiting. Restarting, scaling or rolling back order-service would not help"
            f" and would hide the real cause.{cause} Escalating to a person."
        )
    if s.alert == "ConsumerLagHigh":
        return (
            f"{s.service} is falling behind on its events. Lag is a symptom of a slow or stopped"
            " consumer, not a cause the agent can fix safely. Escalating to a person."
        )
    if s.alert == "DeadLetterMessages":
        return (
            "A message was parked in a dead-letter topic after failing five times. A person must"
            " look at it; no tool would fix the message. Escalating."
        )
    return f"{s.alert} is information, not a call to act. Escalating to a person."
