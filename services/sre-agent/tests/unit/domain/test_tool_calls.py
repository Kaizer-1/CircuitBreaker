"""The menu shown to the model is the allowlist, and its answer is only shape-checked here."""

import pytest

from sre_agent.domain.actions import APPLICATION_SERVICES, SCALABLE, Tool
from sre_agent.domain.tool_calls import UnusableAnswerError, definitions, parse


def test_four_tools_whose_service_enums_are_the_allowlist_and_never_a_data_store() -> None:
    tools = {t["function"]["name"]: t["function"] for t in definitions()}
    assert set(tools) == {t.value for t in Tool}
    enum = {
        name: f["parameters"]["properties"].get("service", {}).get("enum")
        for name, f in tools.items()
    }
    assert set(enum["scale_deployment"]) == SCALABLE
    assert (
        set(enum["rollback_deployment"]) == set(enum["restart_deployment"]) == APPLICATION_SERVICES
    )
    assert enum["do_nothing"] is None
    text = str(definitions()).lower()
    assert not any(store in text for store in ("cassandra", "postgres", "redpanda"))
    replicas = tools["scale_deployment"]["parameters"]["properties"]["replicas"]
    assert (replicas["minimum"], replicas["maximum"]) == (1, 5)


def test_a_tool_call_becomes_a_proposal_and_bad_values_are_left_for_the_guardrails() -> None:
    p = parse(
        "scale_deployment",
        '{"service":"order-service","replicas":5,"explanation":"Load."}',
        "llm:m",
    )
    assert (p.tool, p.service, p.replicas, p.explanation, p.source) == (
        "scale_deployment", "order-service", 5, "Load.", "llm:m")  # fmt: skip
    odd = parse("scale_deployment", {"service": "cassandra", "replicas": "9"}, "llm:m")
    assert (odd.service, odd.replicas, odd.explanation) == ("cassandra", None, "")
    assert parse("do_nothing", {"service": "x", "explanation": "e"}, "llm:m").service is None
    assert parse("delete_everything", "{}", "llm:m").tool == "delete_everything"  # G1 declines


@pytest.mark.parametrize("arguments", ["{not json", "[1, 2]", '"a string"'])
def test_arguments_that_are_not_a_json_object_make_the_answer_unusable(arguments: str) -> None:
    with pytest.raises(UnusableAnswerError):
        parse("restart_deployment", arguments, "llm:m")
