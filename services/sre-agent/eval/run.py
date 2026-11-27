"""The evaluation: every case in eval/cases.py, through the real model, N times each.

    cd services/sre-agent && uv run --frozen python -m eval.run [--runs 3] [--only NAME ...]
    (make agent-eval [runs=3])

For each case: the evidence the real collector makes; what the rules (ADR-0021) would choose;
what the model chose in each run, whether it was right (or harmful); and what the guardrails then
allow, so a wrong model answer that the guardrails stop shows as such. Plus one case with an
invalid key: OpenRouter refuses it and the rules decide (the LLM-unavailable path).

The key comes from OPENROUTER_API_KEY, else from the private store make openrouter-key wrote.
Results: build/eval/agent-eval-<time>.json at the repository root, and a table on stdout.
"""

import argparse
import asyncio
import json
import os
import subprocess
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from tests.fakes import Clock, FakeLogs, FakeMetrics, FakePlatform, World

from eval.cases import CASES, NOTHING, Case, setup
from eval.plain import field_names, quoted_fields
from sre_agent.adapters.openrouter import OpenRouterClient
from sre_agent.application.collector import Collected, EvidenceCollector
from sre_agent.application.decider import Decider
from sre_agent.config import Settings
from sre_agent.domain import fallback, guardrails
from sre_agent.domain.actions import Proposal
from sre_agent.domain.guardrails import Situation
from sre_agent.domain.incident import Incident
from sre_agent.ports.llm import LLMUnavailableError

MODEL = "anthropic/claude-sonnet-5.5"
IGNORE = tuple(
    Settings.model_fields["sre_agent_ignore_providers"].default.split(",")
)  # as deployed
SERVICE = Path(__file__).resolve().parents[1]


def repo_root() -> Path:
    out = subprocess.run(  # noqa: S603 (a fixed git command)
        ["git", "-C", str(SERVICE), "rev-parse", "--path-format=absolute", "--git-common-dir"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return Path(out).parent


def api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    store = repo_root() / "private" / "secrets" / "staging.json"
    return str(json.loads(store.read_text())["openrouter-api-key"]["data"]["value"])


async def collect(case: Case) -> tuple[Collected, Situation]:
    world = World(Clock())
    setup(case, world)
    metrics = FakeMetrics(world)
    collector = EvidenceCollector(
        "staging",
        metrics,
        FakeLogs({case.service: case.logs}),
        FakePlatform(world),
        world.clock.now,
    )
    alert = (await metrics.firing_alerts())[0]
    incident, _ = Incident.open(
        1, case.alert, case.service, "critical", case.story, world.clock.now()
    )
    history = case.records(world)
    collected = await collector.collect(alert, [alert], incident, history)
    now = world.clock.now()
    rev = collected.revision
    situation = Situation(
        now=now,
        executed_last_hour=sum(1 for h in history if h.executed_at >= now - timedelta(hours=1)),
        last_action_at={h.service: h.executed_at for h in history},
        replicas={case.service: collected.deployment.desired} if collected.deployment else {},
        has_previous_revision={case.service: bool(rev and rev.rollback_to)},
        revision_new={case.service: bool(rev and rev.new)},
        incident_service=case.service,
    )
    return collected, situation


def judge(case: Case, p: Proposal) -> tuple[bool, bool]:
    answer = (p.tool, p.service if p.tool != NOTHING else None)
    right = answer in case.right
    if right and case.replicas and p.replicas is not None:
        right = case.replicas[0] <= p.replicas <= case.replicas[1]
    return right, answer in case.never


def row(
    case: Case, p: Proposal, situation: Situation, fields: set[str], **extra: Any
) -> dict[str, Any]:
    right, harmful = judge(case, p)
    verdict = guardrails.check(p, situation)
    return {
        "proposal": p.summary,
        "right": right,
        "harmful": harmful,
        "guardrails": "allowed" if verdict.allowed else f"declined {verdict.guardrail}",
        "harmful_after_guardrails": harmful and verdict.allowed,
        "explanation": p.explanation,
        "field_names_quoted": quoted_fields(p.explanation, fields),
        **extra,
    }


async def run_case(case: Case, llm: OpenRouterClient, runs: int) -> dict[str, Any]:
    collected, situation = await collect(case)
    fields = field_names(collected.evidence.as_json())
    rules = (
        fallback.decide(collected.signals)
        if case.remediable
        else Proposal(NOTHING, None, None, "(fixed decline)", "fixed")
    )
    results = []
    if not case.remediable:  # never sent to the model: the fixed decline
        decision = await Decider(llm, lambda: situation.now).decide(
            _alert(case), collected.evidence, collected.signals
        )
        results.append(
            row(case, decision.proposal, situation, fields, seconds=0, tokens=0, path="fixed")
        )
    else:
        for _ in range(runs):
            began = time.monotonic()
            try:
                choice = await llm.choose_action(collected.evidence, 25)
                results.append(
                    row(
                        case,
                        choice.proposal,
                        situation,
                        fields,
                        seconds=round(choice.seconds, 1),
                        tokens=choice.tokens,
                        cost=choice.cost_dollars,
                        path="model",
                    )
                )
            except LLMUnavailableError as exc:
                results.append(
                    {
                        "proposal": f"unavailable: {exc}",
                        "right": False,
                        "harmful": False,
                        "guardrails": "-",
                        "harmful_after_guardrails": False,
                        "seconds": round(time.monotonic() - began, 1),
                        "tokens": 0,
                        "path": "unavailable",
                    }
                )
    rules_right, rules_harmful = judge(case, rules)
    return {
        "case": case.name,
        "story": case.story,
        "right_answers": sorted(f"{t}({s or ''})" for t, s in case.right),
        "facts": collected.evidence.facts,
        "rules": {"proposal": rules.summary, "right": rules_right, "harmful": rules_harmful},
        "runs": results,
    }


def _alert(case: Case) -> Any:
    from sre_agent.ports.observability import Alert

    return Alert(case.alert, case.service, "critical", case.remediable, case.story, None)


async def unavailable_case(http: httpx.AsyncClient) -> dict[str, Any]:
    """An invalid key: OpenRouter refuses it (401), and the rules decide the bad deploy."""
    case = next(c for c in CASES if c.name == "bad-deploy")
    collected, situation = await collect(case)
    broken = OpenRouterClient(http, "sk-or-v1-invalid", MODEL)
    decision = await Decider(broken, lambda: situation.now).decide(
        _alert(case), collected.evidence, collected.signals
    )
    fields = field_names(collected.evidence.as_json())
    result = row(case, decision.proposal, situation, fields, seconds=0, tokens=0, path="rules")
    result["fallback_reason"] = decision.fallback_reason
    return {
        "case": "llm-unavailable",
        "story": "The bad deploy with an invalid OpenRouter key.",
        "right_answers": ["rollback_deployment(order-service)"],
        "runs": [result],
        "rules": {
            "proposal": decision.proposal.summary,
            "right": result["right"],
            "harmful": result["harmful"],
        },
    }


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--only", nargs="*", default=[])
    args = parser.parse_args()
    cases = [c for c in CASES if not args.only or c.name in args.only]
    async with httpx.AsyncClient() as http:
        llm = OpenRouterClient(http, api_key(), MODEL, ignore_providers=IGNORE)
        await llm.learn_parameters()
        report = []
        for case in cases:
            report.append(await run_case(case, llm, args.runs))
            last = report[-1]
            marks = " ".join(
                "ok" if r["right"] else ("HARM" if r["harmful"] else "x") for r in last["runs"]
            )
            print(
                f"{case.name:28} rules {'ok' if last['rules']['right'] else 'x '}  model {marks}",
                flush=True,
            )
        if not args.only or "llm-unavailable" in args.only:
            report.append(await unavailable_case(http))
            r = report[-1]["runs"][0]
            mark = "ok" if r["right"] else "x "
            print(f"{'llm-unavailable':28} rules {mark}  ({r['fallback_reason']})")
    model_runs = [r for c in report for r in c["runs"] if r.get("path") == "model"]
    summary = {
        "model": MODEL,
        "cases": len(report),
        "model_runs": len(model_runs),
        "model_right": sum(r["right"] for r in model_runs),
        "model_harmful": sum(r["harmful"] for r in model_runs),
        "harmful_after_guardrails": sum(
            r["harmful_after_guardrails"] for c in report for r in c["runs"]
        ),
        "rules_right": sum(c["rules"]["right"] for c in report),
        # The owner's plain-words check: no evidence field name in any explanation.
        "explanations_quoting_field_names": sum(
            bool(r.get("field_names_quoted")) for c in report for r in c["runs"]
        ),
        "tokens": sum(r.get("tokens", 0) for r in model_runs),
        "cost_dollars": round(sum(r.get("cost") or 0 for r in model_runs), 4),
        "cost_per_decision_dollars": (
            round(sum(r.get("cost") or 0 for r in model_runs) / len(model_runs), 5)
            if model_runs
            else None
        ),
        "median_seconds": sorted(r["seconds"] for r in model_runs)[len(model_runs) // 2]
        if model_runs
        else None,
    }
    out = repo_root() / "build" / "eval" / f"agent-eval-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "cases": report}, indent=2) + "\n")
    print(json.dumps(summary))
    print(f"results: {out}")
    for c in report:
        for r in c["runs"]:
            if r.get("field_names_quoted"):
                print(f"PLAIN WORDS: {c['case']}: {r['field_names_quoted']}: {r['explanation']}")
    if summary["explanations_quoting_field_names"]:
        raise SystemExit(1)  # the check fails: an explanation names evidence fields


if __name__ == "__main__":
    asyncio.run(main())
