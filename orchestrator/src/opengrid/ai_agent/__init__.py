"""`opengrid.ai_agent` -- the advisory operator copilot (issue #26). Owner: ui-a/ai.

**Advisory only.** This package never commands, approves, signs or releases anything, and it imports
no write or command module (a test enforces that). It reads a snapshot the API router assembles through
the console's own read-only GET paths, and returns text with citations. Every action it suggests is a
suggestion a human then performs through the normal two-step console flow.

The answer comes from the cheapest tier that can honestly give it:

1. **deterministic** -- no model at all. Most operator questions are lookups the console already made to
   draw the screen, and answering those in code is what makes AI-off parity structural (UI-GLB-08).
2. **routed** -- one screening call (Claude Haiku, strict tool output) classifies the question and
   scores its prompt-injection risk. If screening is unavailable the injection risk is NOT assumed to
   be zero: the copilot stays on the no-model answers.
3. **prose** -- Claude Opus writes an explanation, only for genuinely open-ended "why" questions, and
   only over evidence the console already gathered and cited.

Claude is the primary provider. TypeSafe is an opt-in fallback (`[ai_agent].fallback_enabled`, default
false); both go through `gateway.ModelGateway`, the single path for redaction, budgets and call records.
Every answer is traced; if the trace cannot be written the operator gets "assistant unavailable", never
an untraced answer.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from opengrid.ai_agent import deterministic
from opengrid.ai_agent.budgets import Budget, BudgetLimits, Pricing
from opengrid.ai_agent.gateway import ModelGateway
from opengrid.ai_agent.providers import ModelProvider, ModelRequest
from opengrid.ai_agent.redaction import contains_personal_data, redact_text
from opengrid.ai_agent.router import INJECTION_THRESHOLD
from opengrid.ai_agent.systemone import SystemOneClient, TypeSafeProvider
from opengrid.ai_agent.types import (
    Citation,
    ConfidenceLabel,
    ConfigReader,
    CopilotAnswer,
    ModelCall,
)

logger = logging.getLogger(__name__)

#: Writes one trace record and returns its trace id. Raising means the record was not written.
TraceSink = Callable[[dict[str, Any]], Awaitable[str | None]]

_DECLINE_PERSONAL = (
    "That question needs household-level personal data, which this console never sends to a model. "
    "Ask about a hub, a bank or an obligation instead."
)
_DECLINE_INJECTION = (
    "That text looks like an instruction to the assistant rather than a question about the fleet, so it "
    "was not sent. Rephrase it as a question."
)
_DECLINE_SCOPE = "That is outside what this console knows about."
_DECLINE_ACTION = (
    "The assistant is advisory and cannot command anything. Use the screen's own two-step control, which "
    "the guardian checks and signs."
)
_NO_ANSWER = (
    "I can only answer from what this console holds, and nothing here answers that. Try asking which "
    "obligations are at risk, why an offer was declined, or how the fleet is."
)
UNAVAILABLE = "assistant unavailable -- deterministic controls unaffected"


def _unavailable(reason: str) -> CopilotAnswer:
    return CopilotAnswer(text=f"{UNAVAILABLE} ({reason}).", tier="unavailable", refusal_reason=reason)


class CopilotService:
    """Holds the configuration for one process. Construct via `configure`, or with fakes in tests."""

    def __init__(self, *, gateway: ModelGateway | None = None, budget: Budget | None = None) -> None:
        if gateway is None:
            gateway = ModelGateway(primary=None, budget=budget or Budget(BudgetLimits()))
        self._gateway = gateway

    @property
    def budget(self) -> Budget:
        return self._gateway.budget

    def status(self) -> dict[str, Any]:
        """What System Health renders (UI-DAT-05): what works, and how much budget is left."""
        return {
            "available": self._gateway.available,
            "providers": self._gateway.status(),
            "budget": self.budget.headroom(),
        }

    async def ask(
        self,
        question: str,
        context: dict[str, Any],
        *,
        trace: TraceSink,
        screen: str | None = None,
        user: str | None = None,
    ) -> CopilotAnswer:
        """Answer `question` over `context` and trace it. Never raises. If the trace cannot be written
        the answer is withheld and "assistant unavailable" is returned instead."""
        screened = redact_text((question or "").strip())
        answer = await self._answer(screened.text, context)
        record = _trace_record(
            answer, question=screened.text, redactions=screened.found, screen=screen, user=user
        )
        try:
            trace_id = await trace(record)
        except Exception as exc:
            logger.warning("copilot answer withheld: the trace write failed (%s)", type(exc).__name__)
            return _unavailable("the interaction could not be traced")
        answer.trace_id = trace_id
        return answer

    async def _answer(self, question: str, context: dict[str, Any]) -> CopilotAnswer:
        if not question:
            return CopilotAnswer(
                text="Ask a question about the fleet, the market or a decision.", tier="declined"
            )

        # Personal data in the snapshot is refused before anything else looks at the question, by this
        # layer rather than by any model's discretion (UI spec S3.0(h), product decision D5). Personal
        # data typed into the question itself has already been replaced by `redact_text`.
        if contains_personal_data(context):
            return CopilotAnswer(text=_DECLINE_PERSONAL, tier="declined", refusal_reason="personal_data")

        # Tier 1 is computed first: it is the answer whenever the models cannot be trusted or reached.
        early = deterministic.answer(question, context)

        if not self._gateway.available:
            return early or _unavailable("no model provider is configured")

        request = ModelRequest.build(question, context)
        screened = await self._gateway.screen(request)
        calls: list[ModelCall] = list(screened.calls)
        verdict = screened.value
        if verdict is None:
            # Screening unavailable: the injection risk is unknown, not zero. Stay on no-model answers.
            reason = screened.budget_refusal or "screening unavailable"
            fallback = early or _unavailable(reason)
            return _with_calls(fallback, calls, request)

        if verdict.injection_risk >= INJECTION_THRESHOLD:
            declined = CopilotAnswer(
                text=_DECLINE_INJECTION,
                tier="declined",
                intent=verdict.intent,
                refusal_reason="prompt_injection",
            )
            return _with_calls(declined, calls, request)
        if verdict.is_confident and verdict.intent == "out_of_scope":
            declined = CopilotAnswer(text=_DECLINE_SCOPE, tier="declined", intent="out_of_scope")
            return _with_calls(declined, calls, request)
        if verdict.is_confident and verdict.intent == "draft_action":
            declined = CopilotAnswer(text=_DECLINE_ACTION, tier="declined", intent="draft_action")
            return _with_calls(declined, calls, request)

        if early is not None:
            early.intent = verdict.intent
            return _with_calls(early, calls, request)

        if verdict.intent == "explain_decision":
            explained = await self._gateway.explain(request)
            calls.extend(explained.calls)
            if explained.value is not None:
                prose = CopilotAnswer(
                    text=explained.value,
                    tier="prose",
                    intent=verdict.intent,
                    model=explained.model,
                    provider=explained.provider,
                    confidence=verdict.intent_confidence,
                    confidence_label=confidence_label(verdict.intent_confidence),
                    citations=[Citation(source="/og/api/health", ref="context", label="live console state")],
                )
                return _with_calls(prose, calls, request)
            if explained.budget_refusal is not None:
                return _with_calls(_unavailable(explained.budget_refusal), calls, request)

        routed = CopilotAnswer(text=_NO_ANSWER, tier="routed", intent=verdict.intent)
        return _with_calls(routed, calls, request)


def _with_calls(answer: CopilotAnswer, calls: list[ModelCall], request: ModelRequest) -> CopilotAnswer:
    answer.model_calls = calls
    answer.payload_sha256 = request.payload_sha256 if calls else None
    return answer


def _trace_record(
    answer: CopilotAnswer,
    *,
    question: str,
    redactions: tuple[str, ...],
    screen: str | None,
    user: str | None,
) -> dict[str, Any]:
    """The AI_INTERACTION trace payload. Only redacted text; the payload hash identifies exactly what
    the models were sent without storing it twice."""
    return {
        "question": question[:500],
        "question_redactions": list(redactions),
        "screen": screen,
        "user": user,
        "tier": answer.tier,
        "intent": answer.intent,
        "provider": answer.provider,
        "model": answer.model,
        "models_called": sorted({f"{c.provider}:{c.model}" for c in answer.model_calls}),
        "model_calls": [c.model_dump() for c in answer.model_calls],
        "input_tokens": sum(c.input_tokens for c in answer.model_calls),
        "output_tokens": sum(c.output_tokens for c in answer.model_calls),
        "usd": round(sum(c.usd for c in answer.model_calls), 6),
        "payload_sha256": answer.payload_sha256,
        "confidence": answer.confidence,
        "refusal_reason": answer.refusal_reason,
        "citations": [c.ref for c in answer.citations][:10],
    }


def confidence_label(confidence: float | None) -> ConfidenceLabel:
    """The word the `AI-assisted` badge shows (UI spec S3.0(h) uses High/Medium/Low, not a number)."""
    if confidence is None or confidence < 0.5:
        return "Low"
    if confidence < 0.85:
        return "Medium"
    return "High"


def _claude_provider(cfg: ConfigReader) -> ModelProvider | None:
    """The primary provider, or None if the SDK is not installed (the console then runs without it)."""
    try:
        from opengrid.ai_agent.claude import ClaudeProvider
    except ImportError:
        logger.warning("the anthropic SDK is not installed; the copilot runs on its no-model tier")
        return None
    return ClaudeProvider(
        routing_model=str(cfg.get("ai_agent.routing_model", "claude-haiku-4-5-20251001")),
        explain_model=str(cfg.get("ai_agent.explain_model", "claude-opus-5-5")),
    )


def build_service(cfg: ConfigReader) -> CopilotService:
    """A service wired from `[ai_agent]` config. Never raises on a missing key: that is a normal state."""
    budget = Budget(BudgetLimits.from_config(cfg))
    fallback_provider = str(cfg.get("ai_agent.fallback_provider", "typesafe"))
    fallback: ModelProvider | None = None
    if fallback_provider == "typesafe":
        fallback = TypeSafeProvider(
            SystemOneClient(
                model=str(cfg.get("ai_agent.fallback_model", "jev-latest")),
                timeout_s=float(cfg.get("ai_agent.fallback_timeout_s", 6.0)),
            )
        )
    screening = str(cfg.get("ai_agent.screening_provider", "claude"))
    gateway = ModelGateway(
        primary=_claude_provider(cfg),
        fallback=fallback,
        fallback_enabled=bool(cfg.get("ai_agent.fallback_enabled", False)),
        screening_provider="typesafe" if screening == "typesafe" else "claude",
        budget=budget,
        pricing=Pricing.from_config(cfg),
    )
    return CopilotService(gateway=gateway)


_service: CopilotService | None = None


def configure(cfg: ConfigReader) -> CopilotService:
    """Wire the copilot for this process from `[ai_agent]` config. Safe to call more than once."""
    global _service
    _service = build_service(cfg)
    return _service


def set_service(service: CopilotService | None) -> None:
    """Test seam: install a service built with fakes."""
    global _service
    _service = service


def service() -> CopilotService:
    """The configured copilot, or an unconfigured one that answers from tier 1 and says it is limited."""
    global _service
    if _service is None:
        _service = CopilotService()
    return _service


__all__ = [
    "UNAVAILABLE",
    "CopilotAnswer",
    "CopilotService",
    "TraceSink",
    "build_service",
    "confidence_label",
    "configure",
    "service",
    "set_service",
]
