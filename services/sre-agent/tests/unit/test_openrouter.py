"""The OpenRouter client: what it sends, and that anything but one usable tool call fails with a
reason (ADR-0021): "unavailable" when no answer came, "malformed" when the answer was unusable."""

import json
from typing import Any

import httpx
import pytest

from sre_agent.adapters.openrouter import OpenRouterClient
from sre_agent.domain.evidence import Evidence
from sre_agent.ports.llm import LLMUnavailableError, MalformedAnswerError

EVIDENCE = Evidence(alert={"name": "OrderServiceHighErrorRate"}, facts={"main_error_type": "X"})
MODEL = "anthropic/claude-sonnet-5.5"


def answer(*calls: dict[str, Any], finish: str = "tool_calls") -> dict[str, Any]:
    tool_calls = [{"type": "function", "function": c} for c in calls]
    return {
        "model": MODEL,
        "choices": [{"finish_reason": finish, "message": {"tool_calls": tool_calls}}],
        "usage": {"total_tokens": 4321, "cost": 0.0089},
    }


SCALE = {
    "name": "scale_deployment",
    "arguments": json.dumps({"service": "order-service", "replicas": 5, "explanation": "Load."}),
}


class Server:
    def __init__(self, *responses: httpx.Response | Exception, models: list[Any] | None = None):
        self.responses = list(responses)
        self.models = models or []
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": self.models})
        assert request.headers["authorization"] == "Bearer sk-or-test"
        self.bodies.append(json.loads(request.content))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def client(server: Server) -> OpenRouterClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(server))
    return OpenRouterClient(http, "sk-or-test", MODEL, base_url="https://or/api/v1")


async def test_one_tool_call_becomes_the_proposal_and_the_request_is_as_designed() -> None:
    server = Server(httpx.Response(200, json=answer(SCALE)))
    choice = await client(server).choose_action(EVIDENCE, 25)
    assert (choice.proposal.tool, choice.proposal.replicas, choice.model, choice.tokens) == (
        "scale_deployment",
        5,
        MODEL,
        4321,
    )
    assert choice.proposal.source == MODEL and choice.cost_dollars == 0.0089
    body = server.bodies[0]
    assert body["tool_choice"] == "auto" and "parallel_tool_calls" not in body  # see the module
    assert body["provider"] == {"require_parameters": True, "data_collection": "deny"}
    assert body["usage"] == {"include": True}  # so OpenRouter says what each call cost
    assert len(body["tools"]) == 4 and "temperature" not in body
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1]["content"].count("<evidence>") == 1


async def test_optional_parameters_are_sent_only_when_the_model_list_offers_them() -> None:
    """Sending one no provider supports makes strict routing fail (404, found 2026-11-26)."""
    offered = [
        {"id": MODEL, "supported_parameters": ["tools", "temperature", "parallel_tool_calls"]}
    ]
    server = Server(httpx.Response(200, json=answer(SCALE)), models=offered)
    c = client(server)
    await c.learn_parameters()
    await c.choose_action(EVIDENCE, 25)
    assert server.bodies[0]["temperature"] == 0
    assert server.bodies[0]["parallel_tool_calls"] is False


# The answer staging got on 2026-11-27 (generation gen-1795810254-3ARvLr7TXnJkx4xtCigv, the leak
# incident INC-0006): the model wrote a sentence, then called a tool (Bedrock's stop reason
# tool_use; OpenRouter counted 322 completion tokens), but the answer held only the sentence.
LOST_CALL = {
    "id": "gen-1795810254-3ARvLr7TXnJkx4xtCigv",
    "provider": "Amazon Bedrock",
    "model": "anthropic/claude-sonnet-5.5-20260928",
    "object": "chat.completion",
    "choices": [
        {
            "logprobs": None,
            "finish_reason": "tool_calls",
            "native_finish_reason": "tool_use",
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "Memory in market-data-service is climbing steadily, and traffic is"
                " normal at about 1 request per second. The revision is known-good: release"
                " 0.5.0 has run for 237 minutes, and the current revision is ",
                "refusal": None,
                "reasoning": None,
            },
        }
    ],
    "usage": {"prompt_tokens": 2258, "completion_tokens": 232, "total_tokens": 2490},
}


async def test_the_lost_tool_call_is_asked_again_elsewhere_and_the_model_decides() -> None:
    server = Server(httpx.Response(200, json=LOST_CALL), httpx.Response(200, json=answer(SCALE)))
    choice = await client(server).choose_action(EVIDENCE, 25)
    assert (choice.proposal.tool, choice.proposal.source, choice.attempts) == (
        "scale_deployment",
        MODEL,
        2,
    )
    first, second = server.bodies
    assert "ignore" not in first["provider"]
    assert second["provider"]["ignore"] == ["Amazon Bedrock"]  # never the same provider twice
    assert second["messages"] == first["messages"] and second["tools"] == first["tools"]


async def test_the_lost_tool_call_twice_is_a_malformed_answer_not_an_unavailable_model() -> None:
    server = Server(httpx.Response(200, json=LOST_CALL), httpx.Response(200, json=LOST_CALL))
    with pytest.raises(MalformedAnswerError) as caught:
        await client(server).choose_action(EVIDENCE, 25)
    assert str(caught.value) == (
        "Amazon Bedrock said the model called a tool but sent no call (asked twice)"
    )
    assert caught.value.label == "malformed model answer"
    assert len(server.bodies) == 2


async def test_no_second_try_when_too_little_of_the_deadline_is_left() -> None:
    server = Server(httpx.Response(200, json=LOST_CALL))
    with pytest.raises(MalformedAnswerError, match="sent no call$"):
        await client(server).choose_action(EVIDENCE, 7)  # a retry needs 8 s
    assert len(server.bodies) == 1


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (answer(finish="stop"), "text and no tool call"),
        (answer(SCALE, SCALE), "2 tool calls"),
        (answer({"name": "restart_deployment", "arguments": "{oops"}), "not JSON"),
        (answer(SCALE, finish="length"), "cut off"),
    ],
)
async def test_anything_but_one_usable_tool_call_is_malformed(
    response: dict[str, Any], reason: str
) -> None:
    server = Server(httpx.Response(200, json=response), httpx.Response(200, json=response))
    with pytest.raises(MalformedAnswerError, match=reason):
        await client(server).choose_action(EVIDENCE, 25)
    assert len(server.bodies) == 2


async def test_no_answer_at_all_is_unavailable_not_malformed() -> None:
    for failure in (httpx.Response(402), httpx.ReadTimeout("slow")):
        with pytest.raises(LLMUnavailableError) as caught:
            await client(Server(failure)).choose_action(EVIDENCE, 25)
        assert not isinstance(caught.value, MalformedAnswerError)
        assert caught.value.label == "LLM unavailable"


async def test_a_provider_error_is_retried_without_that_provider() -> None:
    broken = {"provider": "Some Cloud", "choices": [{"error": {"code": 502}, "message": {}}]}
    server = Server(httpx.Response(200, json=broken), httpx.Response(200, json=answer(SCALE)))
    assert (await client(server).choose_action(EVIDENCE, 25)).attempts == 2
    assert server.bodies[1]["provider"]["ignore"] == ["Some Cloud"]


async def test_a_fast_failure_is_retried_once_and_a_used_up_credit_is_not() -> None:
    server = Server(httpx.Response(429), httpx.Response(200, json=answer(SCALE)))
    assert (await client(server).choose_action(EVIDENCE, 25)).proposal.replicas == 5
    assert len(server.bodies) == 2
    broke = Server(httpx.Response(402))
    with pytest.raises(LLMUnavailableError, match="credit is used up"):
        await client(broke).choose_action(EVIDENCE, 25)
    assert len(broke.bodies) == 1


async def test_a_timeout_is_not_retried_and_says_how_long_it_waited() -> None:
    server = Server(httpx.ReadTimeout("slow"))
    with pytest.raises(LLMUnavailableError, match="did not answer in 25 s"):
        await client(server).choose_action(EVIDENCE, 25)
    assert len(server.bodies) == 1


async def test_providers_to_ignore_are_never_routed_to() -> None:
    server = Server(httpx.Response(200, json=answer(SCALE)))
    http = httpx.AsyncClient(transport=httpx.MockTransport(server))
    c = OpenRouterClient(
        http,
        "sk-or-test",
        MODEL,
        base_url="https://or/api/v1",
        ignore_providers=("Claude Platform on AWS",),
    )
    await c.choose_action(EVIDENCE, 25)
    assert server.bodies[0]["provider"]["ignore"] == ["Claude Platform on AWS"]
