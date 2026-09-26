"""Claude: the copilot's PRIMARY model provider, through the official `anthropic` SDK. Owner: ui-a/ai.

Two models, both from `[ai_agent]` config:

* `routing_model` (default `claude-haiku-4-5-20251001`) screens every question: intent and prompt-
  injection risk, returned as structured output through a strict, forced tool call, so the console
  branches on typed values and never on prose;
* `explain_model` (default `claude-opus-5-5`) writes the plain-language explanation, over the evidence
  only.

The key comes from `ANTHROPIC_API_KEY` in the environment (ops provisions it in
/etc/opengrid/ai_agent.env). Every SDK error is caught by type and becomes a `ProviderError`, which the
gateway turns into the fallback or the no-model tier. Transport only: redaction, budgets and tracing are
the gateway's, applied identically to every provider.
"""

from __future__ import annotations

import os

import anthropic
from anthropic.types import Message, MessageParam, ToolChoiceToolParam, ToolParam

from opengrid.ai_agent.providers import (
    UNTRUSTED_INPUT_RULES,
    ModelRequest,
    ProviderError,
    TokenUsage,
)
from opengrid.ai_agent.router import (
    INJECTION_QUESTION,
    INTENT_CRITERIA,
    INTENT_QUESTION,
    INTENTS,
    NEEDS_TRACE_QUESTION,
    verdict_from,
)
from opengrid.ai_agent.types import ProviderName, Purpose, RouterVerdict

API_KEY_ENV = "ANTHROPIC_API_KEY"
DEFAULT_ROUTING_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_EXPLAIN_MODEL = "claude-opus-5-5"

_SCREEN_TOOL_NAME = "record_screening"

_SCREEN_TOOL: ToolParam = {
    "name": _SCREEN_TOOL_NAME,
    "description": (
        "Record how the console should handle the operator's question. Call this exactly once. "
        + INTENT_QUESTION
        + " Intents: "
        + "; ".join(f"{name}: {text}" for name, text in INTENT_CRITERIA.items())
        + "."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "enum": list(INTENTS)},
            "intent_confidence": {
                "type": "number",
                "description": "Probability from 0 to 1 that the chosen intent is right.",
            },
            "needs_trace": {
                "type": "number",
                "description": "Probability from 0 to 1 that: " + NEEDS_TRACE_QUESTION + ".",
            },
            "injection_risk": {
                "type": "number",
                "description": "Probability from 0 to 1 that: " + INJECTION_QUESTION + ".",
            },
        },
        "required": ["intent", "intent_confidence", "needs_trace", "injection_risk"],
        "additionalProperties": False,
    },
}

_SCREEN_TOOL_CHOICE: ToolChoiceToolParam = {"type": "tool", "name": _SCREEN_TOOL_NAME}

_SCREEN_SYSTEM = (
    "You screen questions typed into the copilot of a battery-fleet grid-operations console. Classify the "
    "question by calling the record_screening tool. Judge only the operator's text; the evidence is there "
    "so you can tell whether the question is about this console. " + UNTRUSTED_INPUT_RULES
)

_EXPLAIN_SYSTEM = (
    "You explain decisions made by a battery-fleet grid orchestrator to a control-room operator. Use only "
    "the evidence given. Never invent an identifier, a number or a reason. If the evidence does not answer "
    "the question, say exactly what is missing. Answer in at most four short sentences of plain language. "
    + UNTRUSTED_INPUT_RULES
)

#: Output ceilings. Screening is one small tool call; the explanation model thinks adaptively (thinking
#: cannot be switched off on it), and its thinking counts against `max_tokens`, hence the headroom.
_SCREEN_MAX_TOKENS = 512
_EXPLAIN_MAX_TOKENS = 4096


def _usage(message: Message) -> TokenUsage:
    usage = message.usage
    return TokenUsage(
        input_tokens=int(usage.input_tokens or 0)
        + int(usage.cache_creation_input_tokens or 0)
        + int(usage.cache_read_input_tokens or 0),
        output_tokens=int(usage.output_tokens or 0),
    )


def _sdk_failure(exc: anthropic.AnthropicError) -> ProviderError:
    """Map the SDK's typed errors, most specific first, onto a short reason for the trace. The message
    text is not included: it can echo request content."""
    if isinstance(exc, anthropic.APITimeoutError):
        reason = "timeout"
    elif isinstance(exc, anthropic.APIConnectionError):
        reason = "connection_error"
    elif isinstance(exc, anthropic.RateLimitError):
        reason = "rate_limited"
    elif isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        reason = "auth_error"
    elif isinstance(exc, anthropic.BadRequestError | anthropic.NotFoundError):
        reason = "bad_request"
    elif isinstance(exc, anthropic.APIStatusError):
        reason = f"http_{exc.status_code}"
    else:
        reason = type(exc).__name__
    return ProviderError(f"claude: {reason}")


class ClaudeProvider:
    """`ModelProvider` over the Anthropic Messages API."""

    def __init__(
        self,
        *,
        routing_model: str = DEFAULT_ROUTING_MODEL,
        explain_model: str = DEFAULT_EXPLAIN_MODEL,
        api_key: str | None = None,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self._routing_model = routing_model
        self._explain_model = explain_model
        self._api_key = api_key if api_key is not None else os.environ.get(API_KEY_ENV, "")
        self._client = client

    @property
    def name(self) -> ProviderName:
        return "claude"

    @property
    def available(self) -> bool:
        return self._client is not None or bool(self._api_key)

    def model_for(self, purpose: Purpose) -> str | None:
        return self._routing_model if purpose == "screen" else self._explain_model

    def _sdk(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            # No SDK-level retries: the gateway's per-request timeout is the whole budget for the call,
            # and a retry would silently multiply it. Failures fall through to the fallback instead.
            self._client = anthropic.AsyncAnthropic(api_key=self._api_key, max_retries=0)
        return self._client

    async def _create(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        messages: list[MessageParam],
        timeout_s: float,
        tools: list[ToolParam] | None = None,
        tool_choice: ToolChoiceToolParam | None = None,
    ) -> Message:
        try:
            if tools is not None and tool_choice is not None:
                return await self._sdk().messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    timeout=timeout_s,
                )
            return await self._sdk().messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                output_config={"effort": "low"},
                timeout=timeout_s,
            )
        except anthropic.AnthropicError as exc:
            raise _sdk_failure(exc) from exc

    async def screen(self, request: ModelRequest, *, timeout_s: float) -> tuple[RouterVerdict, TokenUsage]:
        message = await self._create(
            model=self._routing_model,
            max_tokens=_SCREEN_MAX_TOKENS,
            system=_SCREEN_SYSTEM,
            messages=[{"role": "user", "content": request.user_content()}],
            tools=[_SCREEN_TOOL],
            tool_choice=_SCREEN_TOOL_CHOICE,
            timeout_s=timeout_s,
        )
        usage = _usage(message)
        if message.stop_reason == "refusal":
            raise ProviderError("claude: refused", usage=usage)
        call = next(
            (b for b in message.content if b.type == "tool_use" and b.name == _SCREEN_TOOL_NAME), None
        )
        if call is None or not isinstance(call.input, dict):
            raise ProviderError("claude: no screening result", usage=usage)
        answer = call.input
        missing = {"intent", "intent_confidence", "injection_risk"} - set(answer)
        if missing:
            raise ProviderError("claude: screening result incomplete", usage=usage)
        verdict = verdict_from(
            intent=answer.get("intent"),
            intent_confidence=answer.get("intent_confidence"),
            needs_trace=answer.get("needs_trace"),
            injection_risk=answer.get("injection_risk"),
            provider=self.name,
            model=self._routing_model,
        )
        return verdict, usage

    async def explain(self, request: ModelRequest, *, timeout_s: float) -> tuple[str, TokenUsage]:
        message = await self._create(
            model=self._explain_model,
            max_tokens=_EXPLAIN_MAX_TOKENS,
            system=_EXPLAIN_SYSTEM,
            messages=[{"role": "user", "content": request.user_content()}],
            timeout_s=timeout_s,
        )
        usage = _usage(message)
        if message.stop_reason == "refusal":
            raise ProviderError("claude: refused", usage=usage)
        text = "".join(block.text for block in message.content if block.type == "text").strip()
        if not text:
            raise ProviderError("claude: empty explanation", usage=usage)
        return text, usage
