"""`opengrid.ai_agent` -- the advisory operator copilot (issue #26). Owner: ui-a/ai.

**Advisory only.** This package never commands, approves, signs or releases anything. It reads a
redacted snapshot the API router hands it and returns text with citations. Every action it suggests is
a suggestion a human then performs through the normal two-step console flow.

The answer comes from the cheapest tier that can honestly give it:

1. **deterministic** -- no model at all. Most operator questions are lookups the console already made to
   draw the screen, and answering those in code is what makes AI-off parity structural (UI-GLB-08).
2. **routed** -- one System One call (~200-400 ms measured) classifies the question, screens it for
   prompt injection and says whether the trace is needed. Typed answers with calibrated probabilities,
   so code branches on them rather than on prose.
3. **prose** -- a reasoning model, only for genuinely open-ended explanation, and only over evidence the
   console already gathered and cited.

Every tier fails soft. If the judgement service, the key or the budget is missing, the console still
answers from tier one and says plainly that the assistant is unavailable (UI-DSP-13: a Must, tested).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from opengrid.ai_agent import deterministic
from opengrid.ai_agent.budgets import Budget, BudgetLimits
from opengrid.ai_agent.prose import ProseModel, UnavailableProseModel, build_prose_model
from opengrid.ai_agent.redaction import contains_personal_data, redact
from opengrid.ai_agent.router import INJECTION_THRESHOLD, route
from opengrid.ai_agent.systemone import SystemOneClient
from opengrid.ai_agent.types import (
    Citation,
    ConfidenceLabel,
    ConfigReader,
    CopilotAnswer,
    RouterVerdict,
)

logger = logging.getLogger(__name__)

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
_UNAVAILABLE = "assistant unavailable -- deterministic controls unaffected"


class CopilotService:
    """Holds the configuration for one process. Construct via `configure`."""

    def __init__(
        self,
        *,
        judge: SystemOneClient | None = None,
        prose: ProseModel | None = None,
        budget: Budget | None = None,
    ) -> None:
        self._judge = judge or SystemOneClient()
        self._prose = prose or UnavailableProseModel()
        self._budget = budget or Budget(BudgetLimits())

    @property
    def budget(self) -> Budget:
        return self._budget

    def status(self) -> dict[str, Any]:
        """What System Health renders (UI-DAT-05): what works, and how much budget is left."""
        return {
            "judgement_model": self._judge.model if self._judge.configured else None,
            "judgement_available": self._judge.configured,
            "prose_model": self._prose.name if self._prose.available else None,
            "prose_available": self._prose.available,
            "available": self._judge.configured or self._prose.available,
            "budget": self._budget.headroom(),
        }

    async def ask(self, question: str, context: dict[str, Any]) -> CopilotAnswer:
        """Answer `question` over `context`. Never raises; never blocks longer than the budget timeout."""
        question = (question or "").strip()
        if not question:
            return CopilotAnswer(
                text="Ask a question about the fleet, the market or a decision.", tier="declined"
            )

        # Personal data is refused before anything else looks at the question, and by this layer rather
        # than by any model's discretion (UI spec S3.0(h), product decision D5).
        if contains_personal_data(context) or contains_personal_data({"q": question}):
            return CopilotAnswer(text=_DECLINE_PERSONAL, tier="declined", refusal_reason="personal_data")
        safe_context = redact(context)

        # Tier 1 first: if the console can answer it from what it already holds, nothing else runs.
        early = deterministic.answer(question, context)
        refusal = self._budget.check()

        if refusal is not None:
            if early is not None:
                return early
            return CopilotAnswer(
                text=f"{_UNAVAILABLE} ({refusal}).", tier="unavailable", refusal_reason=refusal
            )

        verdict = await self._routed_verdict(question, safe_context)
        if verdict.injection_risk >= INJECTION_THRESHOLD:
            return CopilotAnswer(
                text=_DECLINE_INJECTION,
                tier="declined",
                intent=verdict.intent,
                refusal_reason="prompt_injection",
            )
        if verdict.is_confident and verdict.intent == "out_of_scope":
            return CopilotAnswer(text=_DECLINE_SCOPE, tier="declined", intent="out_of_scope")
        if verdict.is_confident and verdict.intent == "draft_action":
            return CopilotAnswer(text=_DECLINE_ACTION, tier="declined", intent="draft_action")

        if early is not None:
            early.intent = verdict.intent
            return early

        if verdict.intent == "explain_decision" and self._prose.available:
            explained = await self._explain(question, safe_context, verdict)
            if explained is not None:
                return explained

        return CopilotAnswer(
            text=(
                "I can only answer from what this console holds, and nothing here answers that. Try "
                "asking which obligations are at risk, why an offer was declined, or how the fleet is."
            ),
            tier="routed" if verdict.intent_confidence else "unavailable",
            intent=verdict.intent,
        )

    async def _routed_verdict(self, question: str, safe_context: Any) -> RouterVerdict:
        try:
            async with asyncio.timeout(self._budget.limits.timeout_s):
                return await route(self._judge, question, safe_context)
        except (TimeoutError, Exception) as exc:
            logger.info("copilot routing failed, staying deterministic: %s", exc)
            return RouterVerdict(intent="deterministic_query", intent_confidence=0.0)

    async def _explain(
        self, question: str, safe_context: Any, verdict: RouterVerdict
    ) -> CopilotAnswer | None:
        try:
            async with asyncio.timeout(self._budget.limits.timeout_s):
                text, tokens = await self._prose.explain(
                    question=question, evidence=safe_context, timeout_s=self._budget.limits.timeout_s
                )
        except Exception as exc:
            logger.info("copilot explanation unavailable: %s", exc)
            return None
        self._budget.record(tokens=tokens)
        return CopilotAnswer(
            text=text,
            tier="prose",
            intent=verdict.intent,
            model=self._prose.name,
            confidence=verdict.intent_confidence,
            confidence_label=confidence_label(verdict.intent_confidence),
            citations=[Citation(source="/og/api/health", ref="context", label="live console state")],
        )


def confidence_label(confidence: float | None) -> ConfidenceLabel:
    """The word the `AI-assisted` badge shows (UI spec S3.0(h) uses High/Medium/Low, not a number)."""
    if confidence is None or confidence < 0.5:
        return "Low"
    if confidence < 0.85:
        return "Medium"
    return "High"


_service: CopilotService | None = None


def configure(cfg: ConfigReader) -> CopilotService:
    """Wire the copilot for this process from `[ai_agent]` config. Safe to call more than once."""
    global _service
    read = cfg.get
    _service = CopilotService(
        judge=SystemOneClient(
            model=str(read("ai_agent.judgement_model", "jev-latest")),
            timeout_s=float(read("ai_agent.judgement_timeout_s", 6.0)),
        ),
        prose=build_prose_model(model=str(read("ai_agent.prose_model", "claude-haiku-4-5-20251001"))),
        budget=Budget(BudgetLimits.from_config(cfg)),
    )
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
    "CopilotAnswer",
    "CopilotService",
    "confidence_label",
    "configure",
    "service",
    "set_service",
]
