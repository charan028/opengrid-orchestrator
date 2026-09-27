"""Typed results the copilot returns (issue #26). Owner: ui-a/ai.

Everything the panel renders is one of these; nothing free-form crosses the boundary. A `CopilotAnswer`
always carries the tier that produced it and the citations behind it, because the UI spec forbids an
unsourced assertion (`04-ui/01-ui-ux-specification.md` S3.0(h): "never an unsourced assertion").
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class ConfigReader(Protocol):
    """The slice of `opengrid.platform.config.Config` this package uses. Declared structurally so the
    agent never imports the platform config module, and so tests can pass a plain dict-backed reader."""

    def get(self, path: str, default: Any = None) -> Any: ...


#: Which tier answered. `deterministic` uses no model at all and is what the console shows with AI
#: switched off (UI-GLB-08's AI-off parity), so it is always the preferred answer when it applies.
AnswerTier = Literal["deterministic", "routed", "prose", "declined", "unavailable"]

#: What the operator's question wants, as decided by the System One router. `fleet_query` is a count,
#: total or list of hubs matching conditions, answered by the read-only fleet tool.
Intent = Literal["deterministic_query", "fleet_query", "explain_decision", "draft_action", "out_of_scope"]

#: The fleet tool's closed vocabularies (the owner's words, mapped onto the Fleet filters by the API).
FleetAssetClass = Literal["home", "dual_unit", "substation", "truck"]
FleetHealth = Literal["online", "stale", "degraded", "quarantined", "fault", "offline"]
FleetAvailability = Literal["AVAILABLE", "UNAVAILABLE"]
FleetGroupBy = Literal["none", "zone", "availability", "soc_bucket", "health", "asset_class"]
FleetMetric = Literal["count", "available_kw", "available_kwh", "rated_kw", "rated_kwh"]


class FleetQuery(BaseModel):
    """One read-only fleet question, as filters over the Fleet table. Built only by `fleet.parse` (the
    deterministic parser) or `fleet.from_model` (the routing model's validated extraction); the API turns
    it into bound SQL parameters, never into SQL text."""

    model_config = ConfigDict(frozen=True)

    zones: tuple[str, ...] = ()
    asset_class: FleetAssetClass | None = None
    health: tuple[FleetHealth, ...] = ()
    availability: FleetAvailability | None = None
    #: Percent of rated energy, inclusive bounds.
    soc_min_pct: float | None = None
    soc_max_pct: float | None = None
    #: Rated energy per hub (og.hub.e_kwh) and rated power per hub (og.hub.p_kw), inclusive bounds.
    capacity_min_kwh: float | None = None
    capacity_max_kwh: float | None = None
    power_min_kw: float | None = None
    power_max_kw: float | None = None
    bank: str | None = None
    hw: str | None = None
    fw: str | None = None
    #: Trucks only: at (True) or away from (False) their D-31 home station.
    at_home: bool | None = None
    group_by: FleetGroupBy = "none"
    metric: FleetMetric = "count"

    @property
    def has_filter(self) -> bool:
        return any(value not in (None, ()) for name, value in self if name not in ("group_by", "metric"))


#: Runs one `FleetQuery` against the console's data and returns the tool result (JSON primitives only;
#: never coordinates or personal data). Raises on a failed read.
FleetTool = Callable[[FleetQuery], Awaitable[dict[str, Any]]]

ConfidenceLabel = Literal["High", "Medium", "Low"]

#: Which model provider answered. Claude is the primary; TypeSafe is an opt-in fallback only.
ProviderName = Literal["claude", "typesafe"]

#: What a model call was for. Screening (routing plus prompt-injection risk) always precedes explanation.
Purpose = Literal["screen", "explain"]


class ModelCall(BaseModel):
    """One model call, successful or not, exactly as it was charged against the budget and traced."""

    provider: ProviderName
    model: str
    purpose: Purpose
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
    ok: bool = True
    error: str | None = None


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
    #: The provider whose model produced `text` (None when no model wrote it).
    provider: ProviderName | None = None
    #: Every model call made while answering, including screening and failed attempts.
    model_calls: list[ModelCall] = Field(default_factory=list)
    #: SHA-256 of the redacted payload the models were sent; None when no model was called.
    payload_sha256: str | None = None
    #: The routing model that screened the question (intent + injection risk), when one did. Set even
    #: when the answer text itself came from console data, so the panel can say a model was used.
    screened_by: str | None = None

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
    provider: ProviderName | None = None
    #: For a `fleet_query`, the filters the routing model extracted (already validated), else None.
    fleet: FleetQuery | None = None

    @property
    def is_confident(self) -> bool:
        """Below this the router is guessing, so the console falls back to the deterministic tier rather
        than acting on a coin flip (TypeSafe's confidence-gated routing pattern)."""
        return self.intent_confidence >= 0.6
