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

from opengrid.ai_agent import deterministic, fleet
from opengrid.ai_agent.budgets import Budget, BudgetLimits, Pricing
from opengrid.ai_agent.gateway import ModelGateway
from opengrid.ai_agent.grounding import ungrounded_numbers
from opengrid.ai_agent.providers import ModelProvider, ModelRequest
from opengrid.ai_agent.redaction import contains_personal_data, redact_text
from opengrid.ai_agent.router import INJECTION_THRESHOLD
from opengrid.ai_agent.systemone import SystemOneClient, TypeSafeProvider
from opengrid.ai_agent.types import (
    Citation,
    ConfidenceLabel,
    ConfigReader,
    CopilotAnswer,
    FleetQuery,
    FleetTool,
    ModelCall,
    RouterVerdict,
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
    "obligations are at risk, why an offer was declined, how many hubs are below 30% charge in LZ_NORTH, "
    "or the total available kW in LZ_AEN."
)
_UNGROUNDED = (
    "The explanation cited figures that are not in the console's data, so it was withheld. Ask for the "
    "count or total directly and the console will answer from its own records."
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
        fleet_tool: FleetTool | None = None,
    ) -> CopilotAnswer:
        """Answer `question` over `context` and trace it. Never raises. If the trace cannot be written
        the answer is withheld and "assistant unavailable" is returned instead.

        `fleet_tool` is the API's read-only fleet query (counts, totals, top rows); without it, fleet
        questions fall back to the snapshot's hub health counts."""
        screened = redact_text((question or "").strip())
        answer = await self._answer(screened.text, context, fleet_tool)
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

    async def _answer(
        self, question: str, context: dict[str, Any], fleet_tool: FleetTool | None
    ) -> CopilotAnswer:
        if not question:
            return CopilotAnswer(
                text="Ask a question about the fleet, the market or a decision.", tier="declined"
            )

        # Personal data in the snapshot is refused before anything else looks at the question, by this
        # layer rather than by any model's discretion (UI spec S3.0(h), product decision D5). Personal
        # data typed into the question itself has already been replaced by `redact_text`.
        if contains_personal_data(context):
            return CopilotAnswer(text=_DECLINE_PERSONAL, tier="declined", refusal_reason="personal_data")

        # Tier 1 is computed first: it is the answer whenever the models cannot be trusted or reached. A
        # fleet question the parser recognises is answered from the fleet tool, ahead of the snapshot's
        # generic hub counts.
        # A fleet question whose condition the parser could not read is never answered with a count
        # that ignores the condition -- not the fleet tool's, and not the snapshot's hub totals.
        # The same parse decides every path (no model, screening down, screened): a parsed fleet query is
        # answered from the fleet tool, an unread condition is refused, and a question that names a fleet
        # filter is never answered with the snapshot's whole-fleet hub totals.
        reading = fleet.parse(question)
        parsed = reading if isinstance(reading, FleetQuery) else None
        unparsed = reading if isinstance(reading, fleet.UnparsedCondition) else None
        if unparsed is not None:
            early: CopilotAnswer | None = fleet.not_understood(unparsed)
        elif parsed is not None and _filtered(parsed):
            early = None
        else:
            early = deterministic.answer(question, context)

        if not self._gateway.available:
            fleet_answer = await _fleet_answer(parsed, fleet_tool)
            return fleet_answer or early or _unavailable("no model provider is configured")

        request = ModelRequest.build(question, context)
        screened = await self._gateway.screen(request)
        calls: list[ModelCall] = list(screened.calls)
        verdict = screened.value
        if verdict is None:
            # Screening unavailable: the injection risk is unknown, not zero, so no model is used. The
            # parsed fleet query involves no model (typed values, bound parameters): it still answers.
            reason = screened.budget_refusal or "screening unavailable"
            fallback = await _fleet_answer(parsed, fleet_tool) or early or _unavailable(reason)
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

        # The parser's reading wins; otherwise the routing model's validated extraction (only when it is
        # confident). Either way the query is typed values over a fixed vocabulary, run read-only.
        # The routing model's filters are used only when the parser found no fleet question at all: an
        # unread condition is refused on every path (the model's reading of it is not trusted over ours),
        # and a model query with no filter, grouping or metric would be the whole-fleet total.
        model_query = verdict.fleet if verdict.is_confident and reading is None else None
        if model_query is not None and not _filtered(model_query):
            model_query = None
        query = parsed or model_query
        explaining = verdict.intent == "explain_decision"

        if not explaining:
            fleet_answer = await _fleet_answer(query, fleet_tool)
            if fleet_answer is not None:
                if parsed is None:
                    fleet_answer.tier = "routed"  # the routing model chose the filters; code wrote the text
                return _with_calls(fleet_answer, calls, request)

        if early is not None and (not explaining or query is None):
            early.intent = verdict.intent
            return _with_calls(early, calls, request)

        if explaining:
            return await self._explain(question, context, verdict, query, fleet_tool, calls, request)

        routed = CopilotAnswer(text=_NO_ANSWER, tier="routed", intent=verdict.intent)
        return _with_calls(routed, calls, request)

    async def _explain(
        self,
        question: str,
        context: dict[str, Any],
        verdict: RouterVerdict,
        query: FleetQuery | None,
        fleet_tool: FleetTool | None,
        calls: list[ModelCall],
        request: ModelRequest,
    ) -> CopilotAnswer:
        """Prose over the evidence. A fleet result, when the question names fleet conditions, is added to
        the evidence as query output only; the prose is then checked so that every number it cites is in
        that evidence (`grounding`). Ungrounded prose is withheld in favour of the console's own answer."""
        result = await _run_fleet(query, fleet_tool)
        fleet_answer: CopilotAnswer | None = None
        if query is not None and isinstance(result, dict):
            context = {**context, "fleet": result}
            request = ModelRequest.build(question, context)
            fleet_answer = fleet.render(query, result)
        explained = await self._gateway.explain(request)
        calls.extend(explained.calls)
        if explained.value is not None:
            ungrounded = ungrounded_numbers(explained.value, request.evidence, request.question)
            if ungrounded:
                logger.info("copilot prose withheld: %d number(s) not in the evidence", len(ungrounded))
                fallback = fleet_answer or CopilotAnswer(
                    text=_UNGROUNDED, tier="routed", refusal_reason="ungrounded_numbers"
                )
                fallback.intent = verdict.intent
                return _with_calls(fallback, calls, request)
            citations = [Citation(source="/og/api/health", ref="context", label="live console state")]
            if fleet_answer is not None:
                citations = fleet_answer.citations + citations
            prose = CopilotAnswer(
                text=explained.value,
                tier="prose",
                intent=verdict.intent,
                model=explained.model,
                provider=explained.provider,
                confidence=verdict.intent_confidence,
                confidence_label=confidence_label(verdict.intent_confidence),
                citations=citations,
            )
            return _with_calls(prose, calls, request)
        if explained.budget_refusal is not None:
            return _with_calls(_unavailable(explained.budget_refusal), calls, request)
        if fleet_answer is not None:
            return _with_calls(fleet_answer, calls, request)
        routed = CopilotAnswer(text=_NO_ANSWER, tier="routed", intent=verdict.intent)
        return _with_calls(routed, calls, request)


async def _run_fleet(query: FleetQuery | None, tool: FleetTool | None) -> dict[str, Any] | Exception | None:
    """The fleet tool's result for `query`; the exception when the read failed; None when there is no
    query or no tool. A result carrying a personal-data field is discarded (never sent, never shown)."""
    if query is None or tool is None:
        return None
    try:
        result = await tool(query)
    except Exception as exc:
        logger.info("copilot fleet tool unavailable (%s)", type(exc).__name__)
        return exc
    if contains_personal_data(result):
        logger.warning("copilot fleet tool returned a personal-data field; result discarded")
        return None
    return result


async def _fleet_answer(query: FleetQuery | None, tool: FleetTool | None) -> CopilotAnswer | None:
    """The deterministic fleet answer; "can't verify" when the read failed, or when there is no tool for
    a filtered question (whose honest answer is never the snapshot's whole-fleet count); None when there
    is no query, or an unfiltered one and no tool."""
    result = await _run_fleet(query, tool)
    if query is None:
        return None
    if result is None:
        return fleet.unavailable() if _filtered(query) else None
    if isinstance(result, Exception):
        return fleet.unavailable()
    return fleet.render(query, result)


def _filtered(query: FleetQuery) -> bool:
    """A query that narrows or shapes the fleet: any filter, a breakdown, or a metric other than count."""
    return query.has_filter or query.group_by != "none" or query.metric != "count"


def _with_calls(answer: CopilotAnswer, calls: list[ModelCall], request: ModelRequest) -> CopilotAnswer:
    answer.model_calls = calls
    answer.payload_sha256 = request.payload_sha256 if calls else None
    answer.screened_by = next((c.model for c in calls if c.purpose == "screen" and c.ok), None)
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
        "screened_by": answer.screened_by,
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
    gateway = ModelGateway(
        primary=_claude_provider(cfg),
        fallback=fallback,
        fallback_enabled=bool(cfg.get("ai_agent.fallback_enabled", False)),
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
    "FleetQuery",
    "FleetTool",
    "TraceSink",
    "build_service",
    "confidence_label",
    "configure",
    "service",
    "set_service",
]
