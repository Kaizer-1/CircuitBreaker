"""What the model sees about an incident (SRE agent design section 2), in our own vocabulary.

The evidence collector (application/collector.py) fills it from Prometheus, Loki and Kubernetes:
it is an Anti-Corruption Layer, so the prompt has the same shape every time whatever those
systems answer. Nothing in it comes from chaos-service.

Log lines are untrusted text written partly by users (a nickname can be anything the blocklist
lets through), so before they leave our system they are scrubbed (data minimisation: no trader
IDs, nicknames or tokens) and cut to 300 characters.
"""

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

MAX_LINE = 300
MAX_LINES = 50

_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_NICKNAME = re.compile(r'("?(?:nickname|display_name|trader)"?\s*[:=]\s*)("[^"]*"|[^\s,}]+)', re.I)
_TOKEN = re.compile(r"(bearer\s+|access_token=|token=)[^\s\"&]+", re.I)


def scrub(line: str) -> str:
    """A log line without the people in it, at most 300 characters."""
    line = _TOKEN.sub(r"\1<redacted>", line)
    line = _NICKNAME.sub(r"\1<nickname>", line)
    line = _UUID.sub("<id>", line)
    return line if len(line) <= MAX_LINE else line[: MAX_LINE - 1] + "…"


@dataclass(frozen=True)
class Evidence:
    alert: dict[str, Any]  # name, service, severity, summary, firing_for_seconds
    other_alerts: list[str] = field(default_factory=list)  # also firing for this service
    # The three facts of ADR-0021 (error type, revision, load) and a few more, computed by code.
    facts: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)  # 15 min, a point every 30 s + baseline
    logs: dict[str, Any] = field(default_factory=dict)  # top_errors and recent lines, scrubbed
    deploy_history: list[dict[str, Any]] = field(default_factory=list)
    pods: list[dict[str, Any]] = field(default_factory=list)
    agent_history: dict[str, Any] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)  # sources that did not answer

    def as_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), default=str)

    def summary(self) -> str:
        """One line for the incident (Q-18 `evidence_summary`)."""
        f = self.facts
        parts = []
        if f.get("error_ratio") is not None:
            parts.append(f"Errors {100 * f['error_ratio']:.1f}%")
        errors = f.get("errors_per_second_by_type") or {}
        if errors:
            ranked = sorted(errors.items(), key=lambda kv: -kv[1])
            kinds = ", ".join(f"{k} {v:.1f}/s" for k, v in ranked)
            parts.append(f"by type: {kinds}")
        revision = f.get("revision") or {}
        if revision:
            state = "new" if revision.get("new") else "known-good"
            release = revision.get("release") or "no release name"
            parts.append(f"Revision {revision.get('number')} ({release}, {state})")
        if f.get("request_rate") is not None:
            parts.append(
                f"Requests {f['request_rate']:.1f}/s (normal {f.get('normal_rate') or 0:.1f}/s)"
            )
        if f.get("memory_of_limit"):
            trend = "rising" if f.get("memory_rising") else "flat"
            parts.append(f"Memory {100 * f['memory_of_limit']:.0f}% of limit, {trend}")
        top = (self.logs.get("top_errors") or [None])[0]
        if top:
            parts.append(f"Top log error: {top['message'][:80]} ({top['count']} times)")
        if self.missing:
            parts.append(f"Not available: {', '.join(self.missing)}")
        return ". ".join(parts) + "." if parts else "No evidence collected."
