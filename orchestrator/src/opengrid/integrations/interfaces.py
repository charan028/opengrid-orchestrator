"""Backend-neutral interfaces for external grid and market integrations (protocol-adapters.md S2).

Two seams, each with several interchangeable backends selected by config:

- `ScadaSource`: reads utility bank analogs and status, and receives utility instructions. Every
  backend (MQTT, DNP3, ICCP/TASE.2, IEEE 2030.5) emits the SAME existing wire models,
  `ScadaBankSignal` and `ScadaUtilityInstruction` (`opengrid.core.models.mqtt`), into a `ScadaSink`.
  Downstream consumers (fleet twin, guardian G-03/G-15, health) therefore never learn which protocol
  delivered a reading.
- `MarketSubmission`: submits energy offers, AS offers and three-part supply offers, and receives
  awards and dispatch instructions. The market-neutral types below are what the rest of the
  orchestrator sees; ERCOT-specific XML stays inside `opengrid.integrations.ercot_mms`.

Nothing here does I/O. The concrete adapters own their sockets, TLS and credentials.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from itertools import pairwise
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from opengrid.core.models.mqtt import ScadaBankSignal, ScadaUtilityInstruction

__all__ = [
    "AsOffer",
    "AsService",
    "Award",
    "DispatchInstruction",
    "DispatchInstructionSource",
    "EnergyOffer",
    "InstructionBatch",
    "MalformedInstruction",
    "MarketSubmission",
    "OfferCurvePoint",
    "ScadaSink",
    "ScadaSource",
    "SubmissionReceipt",
    "ThreePartSupplyOffer",
]


# --------------------------------------------------------------------------------------------------
# SCADA
# --------------------------------------------------------------------------------------------------


@runtime_checkable
class ScadaSink(Protocol):
    """Where a `ScadaSource` delivers what it reads. Implementations: `FleetScadaSink` (in-process into
    `opengrid.fleet`), `MqttBridgeSink` (republish on the existing `<root>/scada/...` topics)."""

    async def on_bank_signal(self, signal: ScadaBankSignal) -> None: ...

    async def on_utility_instruction(self, instruction: ScadaUtilityInstruction) -> None: ...


@runtime_checkable
class ScadaSource(Protocol):
    """One utility SCADA backend. `run` is long-lived: it connects, polls or subscribes, reconnects
    with backoff on failure, and returns only when cancelled. `poll_once` performs a single complete
    read (used by tests and by an operator "read now" action) and returns what it delivered."""

    @property
    def backend(self) -> str:
        """Config name of this backend (`mqtt`, `dnp3`, `iccp`, `ieee2030_5`)."""
        ...

    async def run(self, sink: ScadaSink) -> None: ...

    async def poll_once(self, sink: ScadaSink) -> int:
        """One read cycle; returns how many signals/instructions were delivered to `sink`."""
        ...

    async def close(self) -> None: ...


# --------------------------------------------------------------------------------------------------
# Market submission (market-neutral shapes)
# --------------------------------------------------------------------------------------------------

AsService = Literal["REGUP", "REGDN", "RRS", "ECRS", "NSPIN"]
MarketRun = Literal["DAM", "RTM"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OfferCurvePoint(_Model):
    """One price/quantity point. `mw` may be negative only on an ESR bid/offer curve (charging side)."""

    mw: Decimal
    price_usd_per_mwh: Decimal


def _check_monotonic(points: list[OfferCurvePoint]) -> None:
    """Offer curves must be monotonically non-decreasing in both MW and price (ERCOT Protocols 4.4.9.3)."""
    for prev, cur in pairwise(points):
        if cur.mw <= prev.mw:
            raise ValueError("offer curve MW must be strictly increasing")
        if cur.price_usd_per_mwh < prev.price_usd_per_mwh:
            raise ValueError("offer curve price must be non-decreasing")


class EnergyOffer(_Model):
    """An energy offer curve (or, for an ESR under RTC+B, an energy bid/offer curve) for one resource
    over one interval. `curve_kind` selects the ERCOT noun the adapter builds."""

    resource_id: str
    market: MarketRun
    curve_kind: Literal["ENERGY_OFFER", "ESR_BID_OFFER"] = "ENERGY_OFFER"
    interval_start: datetime
    interval_end: datetime
    curve: list[OfferCurvePoint] = Field(min_length=1, max_length=10)
    external_ref: str | None = None  # the orchestrator's own id (e.g. plan/opportunity) for audit

    @model_validator(mode="after")
    def _validate(self) -> EnergyOffer:
        if self.interval_end <= self.interval_start:
            raise ValueError("interval_end must be after interval_start")
        _check_monotonic(self.curve)
        if self.curve_kind == "ENERGY_OFFER" and any(p.mw < 0 for p in self.curve):
            raise ValueError("a plain energy offer curve cannot contain negative MW")
        return self


class AsOffer(_Model):
    """Ancillary-service offer: up to five price/quantity blocks for one service and interval."""

    resource_id: str
    market: MarketRun
    service: AsService
    interval_start: datetime
    interval_end: datetime
    blocks: list[OfferCurvePoint] = Field(min_length=1, max_length=5)
    external_ref: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> AsOffer:
        if self.interval_end <= self.interval_start:
            raise ValueError("interval_end must be after interval_start")
        if any(b.mw <= 0 for b in self.blocks):
            raise ValueError("AS offer blocks must have positive MW")
        _check_monotonic(self.blocks)
        return self


class ThreePartSupplyOffer(_Model):
    """Three-part supply offer (startup offer, minimum-energy offer, energy offer curve). Relevant to
    generation resources; an ESR does not submit one under RTC+B, so the adapter rejects it for ESRs."""

    resource_id: str
    market: MarketRun
    interval_start: datetime
    interval_end: datetime
    startup_cost_usd: Decimal = Field(ge=0)
    min_energy_cost_usd_per_mwh: Decimal
    energy_curve: list[OfferCurvePoint] = Field(min_length=1, max_length=10)
    external_ref: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> ThreePartSupplyOffer:
        if self.interval_end <= self.interval_start:
            raise ValueError("interval_end must be after interval_start")
        _check_monotonic(self.energy_curve)
        return self


class SubmissionReceipt(_Model):
    """The market operator's synchronous answer to one submission."""

    submission_id: str  # our MessageID / mRID, echoed by the operator
    status: Literal["ACCEPTED", "REJECTED", "ERROR"]
    operator_ref: str | None = None  # the operator's transaction id, when returned
    errors: list[str] = []
    received_at: datetime

    @property
    def accepted(self) -> bool:
        return self.status == "ACCEPTED"


class Award(_Model):
    """A cleared award for one resource and interval (energy or AS)."""

    award_id: str
    kind: Literal["ENERGY", "AS"]
    market: MarketRun
    trading_date: date
    resource_id: str
    service: AsService | None = None
    interval_start: datetime
    interval_end: datetime
    awarded_mw: Decimal  # +discharge (sell); negative = ESR charging award
    # MCPC for AS ($/MW-h); for energy the settlement point price when the operator reports it (ERCOT's
    # AwardedEnergyOffer carries MWh only, so it is None there and priced from the DAM SPP feed).
    price_usd_per_mwh: Decimal | None = None

    @model_validator(mode="after")
    def _validate(self) -> Award:
        if (self.kind == "AS") != (self.service is not None):
            raise ValueError("service is required for AS awards and forbidden for energy awards")
        return self


class DispatchInstruction(_Model):
    """A real-time instruction from the market operator: an AS deployment, or a verbal/XML dispatch
    instruction. Base points arrive over ICCP in production and are out of scope for this seam."""

    instruction_id: str
    kind: Literal["AS_DEPLOYMENT", "AS_RECALL", "VDI"]
    resource_id: str
    service: AsService | None = None
    mw: Decimal | None = None
    start_at: datetime
    end_at: datetime | None = None
    issued_at: datetime
    text: str | None = None
    ramp_minutes: int | None = Field(default=None, ge=0)
    recalls: str | None = None  # AS_RECALL: the deployment instruction it ends, when the operator names it

    @model_validator(mode="after")
    def _validate(self) -> DispatchInstruction:
        if not self.instruction_id:
            raise ValueError("instruction_id is required")
        if self.mw is not None and self.mw < 0:
            raise ValueError("mw must not be negative")
        if self.end_at is not None and self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        return self


class MalformedInstruction(_Model):
    """An instruction the adapter received but could not parse. `instruction_id` is None when even the
    id was unreadable (it can then not be acknowledged)."""

    instruction_id: str | None
    error: str


class InstructionBatch(_Model):
    """One poll of the market operator: the parsed instructions and the ones that failed to parse. One
    bad instruction never hides the others."""

    instructions: list[DispatchInstruction] = []
    malformed: list[MalformedInstruction] = []


@runtime_checkable
class DispatchInstructionSource(Protocol):
    """Where AS deployment instructions come from (D-35). `ercot_mms` implements it against ERCOT EWS
    (or the ogsim MMS simulator, same wire); a replacement adapter only has to satisfy this."""

    @property
    def backend(self) -> str: ...

    async def fetch_instruction_batch(self, since: datetime) -> InstructionBatch:
        """Instructions not yet acknowledged, issued at or after `since`. Raises on transport failure."""
        ...

    async def acknowledge_instruction(
        self, instruction_id: str, *, accepted: bool, reason: str | None
    ) -> SubmissionReceipt:
        """Tell the market operator the QSE accepted or rejected (with `reason`) this instruction."""
        ...

    async def close(self) -> None: ...


@runtime_checkable
class MarketSubmission(Protocol):
    """One market-operator submission backend (`ercot_mms`). Submissions are never retried
    automatically: a lost reply is resolved by `fetch_awards`/status queries, not by a resend that
    might double-offer (K3 one-buyer discipline)."""

    @property
    def backend(self) -> str: ...

    async def submit_energy_offer(self, offer: EnergyOffer) -> SubmissionReceipt: ...

    async def submit_as_offer(self, offer: AsOffer) -> SubmissionReceipt: ...

    async def submit_three_part_offer(self, offer: ThreePartSupplyOffer) -> SubmissionReceipt: ...

    async def cancel(self, submission_id: str) -> SubmissionReceipt: ...

    async def fetch_awards(self, trading_date: date) -> list[Award]: ...

    async def fetch_dispatch_instructions(self, since: datetime) -> list[DispatchInstruction]: ...

    async def close(self) -> None: ...
