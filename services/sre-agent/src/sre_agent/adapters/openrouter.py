"""The model through OpenRouter's chat-completions API (ADR-0013, SRE agent design section 4).

One request per decision: the fixed system prompt, the evidence as the user message, the four
tools and `tool_choice: "auto"`, routed only to providers that support every parameter we send
(`provider.require_parameters`) and do not keep the prompts (`provider.data_collection: "deny"`).
`temperature: 0` and `parallel_tool_calls: false` are sent only if OpenRouter's public model list
says the model takes them: for anthropic/claude-sonnet-5.5 no provider offers either (checked
2026-11-26), and sending one anyway makes strict routing find no provider at all (404). Without
`parallel_tool_calls`, an answer with several tool calls is simply unusable (the rules decide).

Everything that is not exactly one tool call with JSON-object arguments raises
LLMUnavailableError with a reason the feed can show (ADR-0021): the rules decide then. All
attempts share one deadline; a second attempt only follows a fast failure (no connection, 429,
5xx) and only when at least 8 s are left.
"""

import asyncio
import logging
import time
from typing import Any

import httpx

from sre_agent.business_metrics import BusinessMetrics
from sre_agent.domain.evidence import Evidence
from sre_agent.domain.prompt import SYSTEM_PROMPT, user_message
from sre_agent.domain.tool_calls import UnusableAnswerError, definitions, parse
from sre_agent.ports.llm import Choice, LLMUnavailableError

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
    pass


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
    ) -> None:
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
            "provider": {"require_parameters": True, "data_collection": "deny"},
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
        last: str = "no attempt"
        for attempt in (1, 2):
            left = deadline_seconds - (time.monotonic() - started)
            if left <= 0 or (attempt == 2 and left < RETRY_NEEDS_SECONDS):
                break
            try:
                choice = await self._attempt(body, left)
                self._observe(started, choice.tokens, choice.model)
                return choice
            except _RetryableError as exc:
                last = str(exc)
                await asyncio.sleep(1.0)
            except LLMUnavailableError:
                self._observe(started, 0, self._model, failed=True)
                raise
        self._observe(started, 0, self._model, failed=True)
        raise LLMUnavailableError(last)

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
        if "error" in choice or "error" in payload:
            raise LLMUnavailableError("the provider reported an error")
        finish = choice.get("finish_reason")
        calls = (choice.get("message") or {}).get("tool_calls") or []
        if finish in ("length", "content_filter"):
            raise LLMUnavailableError(f"the answer was cut off ({finish})")
        if len(calls) != 1:
            what = "no tool call" if not calls else f"{len(calls)} tool calls"
            raise LLMUnavailableError(f"the model answered with {what} (finish: {finish})")
        function = calls[0].get("function") or {}
        model = str(payload.get("model") or self._model)
        try:
            proposal = parse(str(function.get("name", "")), function.get("arguments"), model)
        except UnusableAnswerError as exc:
            raise LLMUnavailableError(str(exc)) from exc
        tokens = int((payload.get("usage") or {}).get("total_tokens") or 0)
        return Choice(proposal, model, time.monotonic() - began, tokens)

    def _observe(self, started: float, tokens: int, model: str, failed: bool = False) -> None:
        if self._metrics is None:
            return
        self._metrics.sre_agent_llm_latency_seconds.observe(time.monotonic() - started)
        if failed:
            self._metrics.sre_agent_llm_errors_total.inc()
        if tokens:
            self._metrics.sre_agent_llm_tokens_total.labels(model).inc(tokens)
