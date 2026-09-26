"""Request/response shapes owned by `api` itself (not already in `opengrid.core.models`) -- 02b S7.1."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from opengrid.core.models.engine import ServiceType as CoreServiceType

# Lower-case, matching the MQTT/URL scope vocabulary (`og/v1/stop/<scope>/<id>`, 02b S6.2) --
# converted to `opengrid.safestop.Scope`'s upper-case literal at the call site.
SafestopScopeName = Literal["fleet", "zone", "bank"]


class _Api(BaseModel):
    model_config = ConfigDict(extra="forbid")


# -- fleet manual command, two-step (02b S7.1/S7.3) --------------------------------------------------


class CommandProposalRequest(_Api):
    bank_id: str | None = None
    hub_id: str | None = None
    p_kw_setpoint: float
    reason: str = Field(min_length=1)


class ProposalAccepted(_Api):
    proposal_id: UUID
    summary: str
    expires_in_s: float


class CommandConfirmResult(_Api):
    proposal_id: UUID
    outcome: str
    vetoed_rule_ids: list[str] = []
    trace_id: UUID | None = None


# -- safe stop, two-step (02b S7.1/S7.3) -------------------------------------------------------------


class SafestopProposalRequest(_Api):
    scope: SafestopScopeName
    scope_id: str | None = None
    reason: str = Field(min_length=1)


class SafestopConfirmResult(_Api):
    proposal_id: UUID
    scope: SafestopScopeName
    scope_id: str | None
    engaged: bool
    trace_id: UUID | None = None


class SafestopReleaseRequest(_Api):
    reason: str = Field(min_length=1)


# -- retention policy edit (operator only) -----------------------------------------------------------


class RetentionPolicyUpdate(_Api):
    event_class: str
    retention_days: int = Field(gt=0)


# -- contracts / opportunities CRUD ------------------------------------------------------------------

ServiceType = CoreServiceType  # one definition: opengrid.core.models.engine
Tier = Literal["L0", "L1", "L2", "T1", "T2", "T3", "T4"]


class ContractCreate(_Api):
    customer_id: UUID
    service_type: ServiceType
    variant: str | None = None
    tier: Tier
    profile_ref: str
    territory_id: UUID | None = None
    start_at: datetime
    end_at: datetime | None = None
    renomination_allowed: bool = False
    penalty_alpha: Decimal | None = None
    penalty_beta: Decimal | None = None
    penalty_theta: Decimal | None = None
    degradation_cost: Decimal = Decimal("0.03")
    fallback_allowed: bool = False


class ContractStatusUpdate(_Api):
    status: Literal["ACTIVE", "SUSPENDED", "ENDED"]


class OpportunityCreate(_Api):
    contract_id: UUID
    window_start: datetime
    window_end: datetime
    requested_kw: Decimal


# -- scenario trigger ---------------------------------------------------------------------------------


class ScenarioTriggerRequest(_Api):
    target_kind: Literal["sim", "asset", "zone", "bank", "hub"] = "sim"
    target_ref: str = "fleet"
    params: dict[str, float | int | str | bool] = {}
    duration_s: int | None = None
