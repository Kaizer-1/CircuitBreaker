"""The four tools as the model is shown them, and how its answer becomes a Proposal.

The definitions are the OpenAI-compatible "function" format OpenRouter accepts (SRE agent design
section 3), built from actions.py so the menu and the allowlist can never differ. `strict` asks the
model to keep to the schema where it can; whatever it returns is still checked by our own code:
here only that it is one tool call with a JSON object as arguments (otherwise the answer is
unusable and the rule-based fallback chooses, ADR-0021), then by the guardrails (an unknown tool,
a data store or 10 replicas are declined there, and recorded).
"""

import json
from typing import Any

from sre_agent.domain.actions import MAX_REPLICAS, TARGETS, Proposal, Tool

EXPLANATION = {
    "type": "string",
    "description": "1 to 4 plain-English sentences for a non-expert audience: what the evidence"
    " shows and why this is the smallest action that fixes the cause.",
}

DESCRIPTIONS = {
    Tool.ROLLBACK: "Roll ONE service back to its previous release. Use when the trouble began with"
    " a NEW revision (first ran minutes ago): INTERNAL_ERROR errors (a bug), crash-looping pods, or"
    " memory climbing since the release. Never when the running revision is known-good (for"
    " example the one just rolled back to).",
    Tool.SCALE: "Change the number of replicas of ONE service (1 to 5, by at most 3 at once). Use"
    " when it is overloaded: errors are OVERLOADED or latency is high, with traffic far above"
    " normal. Also to scale back down once load is normal again.",
    Tool.RESTART: "Restart ONE service's pods (new processes, same release). Use for memory"
    " climbing steadily toward its limit with no new release and normal traffic (a leak), or for"
    " stuck pods.",
    Tool.NOTHING: "Take no action, only record why. Use when the cause is elsewhere (another"
    " service, a dependency, a data store), when it is already recovering, when another action is"
    " still taking effect, or when the evidence is unclear. A person is told.",
}


def definitions() -> list[dict[str, Any]]:
    tools = []
    for tool in Tool:
        properties: dict[str, Any] = {}
        required = []
        if tool is not Tool.NOTHING:
            properties["service"] = {"type": "string", "enum": sorted(TARGETS[tool])}
            required.append("service")
        if tool is Tool.SCALE:
            properties["replicas"] = {"type": "integer", "minimum": 1, "maximum": MAX_REPLICAS}
            required.append("replicas")
        properties["explanation"] = EXPLANATION
        required.append("explanation")
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool.value,
                    "description": DESCRIPTIONS[tool],
                    "strict": True,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                        "additionalProperties": False,
                    },
                },
            }
        )
    return tools


class UnusableAnswerError(Exception):
    """The model's answer is not one tool call with JSON-object arguments."""


def parse(name: str, arguments: str | dict[str, Any] | None, source: str) -> Proposal:
    """One tool call into a Proposal, checking only its shape; the guardrails check the rest."""
    if isinstance(arguments, dict):
        args = arguments
    else:
        try:
            args = json.loads(arguments or "{}")
        except (TypeError, ValueError) as exc:
            raise UnusableAnswerError(f"the arguments of {name!r} are not JSON") from exc
    if not isinstance(args, dict):
        raise UnusableAnswerError(f"the arguments of {name!r} are not an object")
    service = args.get("service")
    replicas = args.get("replicas")
    explanation = args.get("explanation")
    return Proposal(
        tool=str(name),
        service=str(service) if service is not None and name != Tool.NOTHING else None,
        replicas=replicas if isinstance(replicas, int) and not isinstance(replicas, bool) else None,
        explanation=explanation if isinstance(explanation, str) else "",
        source=source,
    )
