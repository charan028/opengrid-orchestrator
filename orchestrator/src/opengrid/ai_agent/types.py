"""Typed results the copilot returns (issue #26). Owner: ui-a/ai.

Everything the panel renders is one of these; nothing free-form crosses the boundary. A `CopilotAnswer`
always carries the tier that produced it and the citations behind it, because the UI spec forbids an
unsourced assertion (`04-ui/01-ui-ux-specification.md` S3.0(h): "never an unsourced assertion").
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


class ConfigReader(Protocol):
    """The slice of `opengrid.platform.config.Config` this package uses. Declared structurally so the
    agent never imports the platform config module, and so tests can pass a plain dict-backed reader."""

    def get(self, path: str, default: Any = None) -> Any: ...


#: Which tier answered. `deterministic` uses no model at all and is what the console shows with AI
#: switched off (UI-GLB-08's AI-off parity), so it is always the preferred answer when it applies.
AnswerTier = Literal["deterministic", "routed", "prose", "declined", "unavailable"]

#: What the operator's question wants, as decided by the System One router.
Intent = Literal["deterministic_query", "explain_decision", "draft_action", "out_of_scope"]

ConfidenceLabel = Literal["High", "Medium", "Low"]


class Citation(BaseModel):
    """Where a claim came from. `source` is the API path or table the value was read from; `ref` is the
    identifier a person can look up (an obligation id, a hub id, a trace id)."""

    source: str
    ref: str
    label: str


class CopilotAnswer(BaseModel):
    """One answer, ready to render. `text` is already plain language; the panel never post-processes it."""

    text: str
    tier: AnswerTier
    citations: list[Citation] = Field(default_factory=list)
    intent: Intent | None = None
    #: The model that produced the answer, for the `AI-assisted` badge. None for the deterministic tier,
    #: which is not AI-assisted and must not carry the badge.
    model: str | None = None
    confidence: float | None = None
    confidence_label: ConfidenceLabel | None = None
    #: Set when the request was refused rather than answered (personal data, injection, budget, scope).
    refusal_reason: str | None = None
    trace_id: str | None = None

    @property
    def is_ai_assisted(self) -> bool:
        return self.model is not None


class Judgement(BaseModel):
    """One System One answer: the typed value plus how concentrated the distribution was."""

    value: Any
    confidence: float | None = None
    probabilities: dict[str, float] = Field(default_factory=dict)


class RouterVerdict(BaseModel):
    """The fast pre-flight on an operator question, before any expensive model is considered."""

    intent: Intent
    intent_confidence: float
    needs_trace: float = 0.0
    injection_risk: float = 0.0
    model: str = "unavailable"

    @property
    def is_confident(self) -> bool:
        """Below this the router is guessing, so the console falls back to the deterministic tier rather
        than acting on a coin flip (TypeSafe's confidence-gated routing pattern)."""
        return self.intent_confidence >= 0.6
