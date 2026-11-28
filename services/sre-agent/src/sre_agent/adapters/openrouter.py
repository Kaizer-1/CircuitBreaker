"""The model through OpenRouter's chat-completions API (ADR-0013, SRE agent design section 4).

One request per decision: the fixed system prompt, the evidence as the user message, the four
tools and `tool_choice: "auto"`, routed only to providers that support every parameter we send
(`provider.require_parameters`) and do not keep the prompts (`provider.data_collection: "deny"`).
"auto" because Claude Sonnet 5.5 refuses forced tool use ("tool_choice: type tool and any are not
supported for this model", checked 2026-11-28; with strict routing "required" finds no provider,
404), so the system prompt says "always call exactly one tool" instead.
A tool call can be lost on the way: on staging (2026-11-27, generation
gen-1795810254-3ARvLr7TXnJkx4xtCigv) the model wrote a sentence and then called a tool (Bedrock's
stop reason `tool_use`, 322 completion tokens), but the answer "Amazon Bedrock" sent back said
finish_reason tool_calls and held only the sentence. Such an answer is asked again once, never
from the same provider.
Providers named in SRE_AGENT_IGNORE_PROVIDERS are never used: "Claude Platform on AWS" sometimes
returned the tool call as text in the message (Anthropic's XML `<invoke ...>` form) while
reporting finish_reason tool_calls (2026-11-26, gen-1795723084-4kPwdr7wxw5ylrcz0kYf). Routing
around a provider is safer than reading actions out of text.
`temperature: 0` and `parallel_tool_calls: false` are sent only if OpenRouter's public model list
says the model takes them: for anthropic/claude-sonnet-5.5 no provider offers either (checked
2026-11-26), and sending one anyway makes strict routing find no provider at all (404). Without
`parallel_tool_calls`, an answer with several tool calls is simply unusable.

Two kinds of failure, labelled differently on the feed (ADR-0021): no answer at all
(LLMUnavailableError: unreachable, too slow, refused, no credit) and an answer that is not
exactly one tool call with JSON-object arguments (MalformedAnswerError). All attempts share one
deadline; a second attempt follows a fast failure (no connection, 429, 5xx, a provider error) or
a malformed answer, only when at least 8 s are left, and never goes back to the provider whose
answer was malformed.
"""

import asyncio
import dataclasses
import logging
import time
from typing import Any

import httpx

from sre_agent.business_metrics import BusinessMetrics
from sre_agent.domain.evidence import Evidence
from sre_agent.domain.prompt import SYSTEM_PROMPT, user_message
from sre_agent.domain.tool_calls import UnusableAnswerError, definitions, parse
from sre_agent.ports.llm import Choice, LLMUnavailableError, MalformedAnswerError

logger = logging.getLogger("openrouter")

API = "https://openrouter.ai/api/v1"
MAX_TOKENS = 700
RETRY_NEEDS_SECONDS = 8.0
REASONS = {
    401: "OpenRouter refused the API key",
    402: "the OpenRouter credit is used up (the key's limit)",
    403: "OpenRouter's moderation refused the request",
    404: "no provider offers the model with these parameters",
}


class _RetryableError(Exception):
    def __init__(self, reason: str, provider: str | None = None) -> None:
        super().__init__(reason)
        self.provider = provider


class OpenRouterClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        model: str,
        *,
        fallback_models: tuple[str, ...] = (),
        base_url: str = API,
        metrics: BusinessMetrics | None = None,
        ignore_providers: tuple[str, ...] = (),
    ) -> None:
        self._ignore = ignore_providers
        self._client = client
        self._key = api_key
        self._model = model
        self._fallbacks = fallback_models
        self._base = base_url.rstrip("/")
        self._metrics = metrics
        self._supported: set[str] = set()  # optional parameters the model list says it takes

    @property
    def model(self) -> str:
        return self._model

    async def learn_parameters(self) -> None:
        """Ask the public model list (no key) which parameters the model takes. Best effort."""
        try:
            response = await self._client.get(f"{self._base}/models", timeout=10.0)
            response.raise_for_status()
            entry = next((m for m in response.json()["data"] if m["id"] == self._model), None)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            logger.warning("could not read the model list", extra={"error": str(exc)})
            return
        if entry is None:
            logger.warning("the model is not in OpenRouter's list", extra={"model": self._model})
            return
        self._supported = set(entry.get("supported_parameters", []))

    def request_body(self, evidence: Evidence) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message(evidence.as_json())},
            ],
            "tools": definitions(),
            "tool_choice": "auto",
            "max_tokens": MAX_TOKENS,
            "provider": {
                "require_parameters": True,
                "data_collection": "deny",
                **({"ignore": list(self._ignore)} if self._ignore else {}),
            },
            # OpenRouter's own field (not a provider parameter): the answer says what the call cost.
            "usage": {"include": True},
        }
        if self._fallbacks:
            body["models"] = [self._model, *self._fallbacks]
        if "temperature" in self._supported:
            body["temperature"] = 0
        if "parallel_tool_calls" in self._supported:
            body["parallel_tool_calls"] = False
        return body

    async def choose_action(self, evidence: Evidence, deadline_seconds: float) -> Choice:
        started = time.monotonic()
        body = self.request_body(evidence)
        failure: LLMUnavailableError = LLMUnavailableError("no attempt")
        tried = 0
        for attempt in (1, 2):
            left = deadline_seconds - (time.monotonic() - started)
            if left <= 0 or (attempt == 2 and left < RETRY_NEEDS_SECONDS):
                break
            tried = attempt
            try:
                choice = await self._attempt(body, left)
            except _RetryableError as exc:
                failure = LLMUnavailableError(str(exc))
                body = self._without(body, exc.provider)
                await asyncio.sleep(1.0)
                continue
            except MalformedAnswerError as exc:
                failure = exc
                body = self._without(body, exc.provider)
                continue
            except LLMUnavailableError:
                self._observe(started, 0, self._model, None, failed=True)
                raise
            if attempt == 2:
                logger.warning("the second try gave a usable answer", extra={"first": str(failure)})
            choice = dataclasses.replace(choice, attempts=attempt)
            self._observe(started, choice.tokens, choice.model, choice.cost_dollars)
            return choice
        self._observe(started, 0, self._model, None, failed=True)
        if tried == 2 and isinstance(failure, MalformedAnswerError):
            raise MalformedAnswerError(f"{failure} (asked twice)", failure.provider)
        raise failure

    @staticmethod
    def _without(body: dict[str, Any], provider: str | None) -> dict[str, Any]:
        """The same request, never routed to `provider` again (it just failed us)."""
        if not provider:
            return body
        ignored = list(body["provider"].get("ignore", []))
        if provider in ignored:
            return body
        return {**body, "provider": {**body["provider"], "ignore": [*ignored, provider]}}

    async def _attempt(self, body: dict[str, Any], seconds: float) -> Choice:
        began = time.monotonic()
        try:
            response = await self._client.post(
                f"{self._base}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self._key}"},
                timeout=seconds,
            )
        except httpx.TimeoutException as exc:
            raise LLMUnavailableError(f"OpenRouter did not answer in {seconds:.0f} s") from exc
        except httpx.HTTPError as exc:
            raise _RetryableError(f"OpenRouter unreachable ({type(exc).__name__})") from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise _RetryableError(f"OpenRouter answered {response.status_code}")
        if response.status_code >= 400:
            reason = REASONS.get(
                response.status_code, f"OpenRouter answered {response.status_code}"
            )
            raise LLMUnavailableError(reason)
        try:
            payload = response.json()
            choice = payload["choices"][0]
        except (ValueError, KeyError, IndexError) as exc:
            raise LLMUnavailableError("OpenRouter's answer had no choice in it") from exc
        provider = payload.get("provider")
        if "error" in choice or "error" in payload:
            raise _RetryableError("the provider reported an error", provider)
        finish = choice.get("finish_reason")
        message = choice.get("message") or {}
        calls = message.get("tool_calls") or []
        if finish in ("length", "content_filter") or len(calls) != 1:
            # What came back, to tell a model's habit from a provider's fault (no evidence in it).
            logger.warning(
                "unusable answer",
                extra={
                    "generation": payload.get("id"),
                    "provider": provider,
                    "finish": finish,
                    "native_finish": choice.get("native_finish_reason"),
                    "choices": len(payload.get("choices") or []),
                    "message_keys": sorted(message),
                    "content": str(message.get("content") or "")[:200],
                },
            )
        if finish in ("length", "content_filter"):
            raise MalformedAnswerError(f"the answer was cut off ({finish})", provider)
        if not calls and finish == "tool_calls":
            raise MalformedAnswerError(
                f"{provider or 'the provider'} said the model called a tool but sent no call",
                provider,
            )
        if len(calls) != 1:
            what = "text and no tool call" if not calls else f"{len(calls)} tool calls"
            raise MalformedAnswerError(f"the model answered with {what}", provider)
        function = calls[0].get("function") or {}
        model = str(payload.get("model") or self._model)
        try:
            proposal = parse(str(function.get("name", "")), function.get("arguments"), model)
        except UnusableAnswerError as exc:
            raise MalformedAnswerError(str(exc), provider) from exc
        usage = payload.get("usage") or {}
        tokens = int(usage.get("total_tokens") or 0)
        cost = usage.get("cost")
        cost_dollars = float(cost) if isinstance(cost, int | float) else None
        return Choice(proposal, model, time.monotonic() - began, tokens, cost_dollars)

    def _observe(
        self,
        started: float,
        tokens: int,
        model: str,
        cost: float | None = None,
        failed: bool = False,
    ) -> None:
        if self._metrics is None:
            return
        self._metrics.sre_agent_llm_latency_seconds.observe(time.monotonic() - started)
        if failed:
            self._metrics.sre_agent_llm_errors_total.inc()
        if tokens:
            self._metrics.sre_agent_llm_tokens_total.labels(model).inc(tokens)
        if cost:
            self._metrics.sre_agent_llm_cost_dollars_total.labels(model).inc(cost)
