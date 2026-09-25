"""Pure-logic data shapes for the allocator's 2-second cycle (02a S5).

These are plain, hashable dataclasses -- not the pydantic `og.*` row models in
`opengrid.core.models.engine` -- because the cycle's hot path runs over thousands of hubs every 2 s
and must stay allocation-light (numpy-friendly floats, no DB/ORM objects). The thin adapter in
`opengrid.allocator.__init__` is the only place that converts between these and the `Grant` row model.

Units: power in kW, apparent power in kVA, money in $/MWh, time in seconds unless named otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Tier = Literal["T1", "T2", "T3", "T4"]
TIER_ORDER: tuple[Tier, ...] = ("T1", "T2", "T3", "T4")


@dataclass(frozen=True, slots=True)
class HubSnapshot:
    """One hub's real-time eligibility state for a bank this cycle (02b `fleet` output)."""

    hub_id: str
    bank_id: str
    free_discharge_kw: float  # already reserve-safe (K1): fleet derived this via hub_capability()
    tau: float = 1.0  # priority/duration weight for water-filling (S5.5)
    served_last_cycle: bool = False
    health: Literal["OK", "STALE", "FAULT", "LAGGING"] = "OK"

    @property
    def is_healthy(self) -> bool:
        return self.health == "OK"


@dataclass(frozen=True, slots=True)
class BankSnapshot:
    """A bank's current physical envelope this cycle (02b `fleet.capability`)."""

    bank_id: str
    capability_kw: float  # cap_{b,t}: total deliverable discharge this interval, reserve-safe
    load_kva: float = 0.0  # current apparent-power loading, for the DIST_DEFERRAL PI / G-03
    kva_rating: float = 0.0
    reserve_kva: float = 0.0
    tau_eff: float = 60.0  # effective time constant for the PI's Ki gain (02a S5.4)
    zone: str = ""  # ERCOT-load-zone-equivalent grouping, for ZONE-scoped L2 instructions / safe-stop


@dataclass(frozen=True, slots=True)
class ObligationCall:
    """A committed obligation's demand on one bank this cycle (02a S5.1's `active_calls`).

    `committed_kw` is the frozen K13 floor (`commitment.committed_kw`); the allocator never proposes
    a grant below `min(committed_kw, prior_granted_kw)` without an override reason code.
    """

    obligation_id: str
    bank_id: str
    service_type: Literal["HOME", "ERCOT_ENERGY", "ERCOT_AS", "DIST_DEFERRAL", "PARTNER_CAPACITY"]
    tier: Tier
    committed_kw: float
    eligible_hub_ids: tuple[str, ...]
    prior_granted_kw: float | None = None
    value_per_mwh: float = 0.0


@dataclass(frozen=True, slots=True)
class ScadaSample:
    """One bank's current SCADA reading, feeding the `DIST_DEFERRAL` PI loop (02a S5.4)."""

    bank_id: str
    apparent_power_kva: float
    reactive_power_kvar: float = 0.0
    forecast_reactive_kvar_next: float = 0.0
    error_kw: float = 0.0
    sigma_n: float = 0.0
    sigma_x: float = 0.0
    beta_x: float = 1.0
    s_q: float = 0.1  # kvar/kW default, A-DE-42


@dataclass(frozen=True, slots=True)
class Instruction:
    """An L2 grid-authority instruction (utility/ISO), a hard constraint never traded (K5)."""

    scope: Literal["BANK", "ZONE", "FLEET"]
    scope_ref: str
    kind: Literal["LIMIT", "BLOCK", "ESTOP"]
    limit_kw: float | None = None


@dataclass(frozen=True, slots=True)
class PriceSignal:
    bank_id: str
    price_usd_per_mwh: float


@dataclass
class DwellState:
    """S6's 5-min-dwell / $5-per-MWh-hysteresis mode tracker for the free-headroom price response.
    Mutable and carried by the caller across cycles -- exactly one instance per bank.
    """

    mode: Literal["LOW", "HIGH"] = "LOW"
    last_switch_at: datetime | None = None


@dataclass
class PiState:
    """One `DistDeferralPI` loop's persistent state, one instance per bank (K9: exactly one
    integrating controller for bank kVA). Carried by the caller across cycles.
    """

    integral: float = 0.0
    n_tilde: float = 0.0
    prev_output_kw: float = 0.0
    state: Literal["STANDBY", "HOLD", "SCHEDULE", "FROZEN"] = "STANDBY"


@dataclass(frozen=True, slots=True)
class FleetState:
    hubs: tuple[HubSnapshot, ...]
    banks: tuple[BankSnapshot, ...]


@dataclass(frozen=True, slots=True)
class LedgerView:
    """Read-only view of committed calls this cycle (02a S1's `active_calls`); the allocator never
    writes the ledger directly -- that stays the ledger module's job.
    """

    calls: tuple[ObligationCall, ...]


@dataclass(frozen=True, slots=True)
class Schedule:
    prices: tuple[PriceSignal, ...] = ()


@dataclass(frozen=True, slots=True)
class ProposedGrant:
    """One row of the proposed command batch the allocator hands to the guardian (S7)."""

    bank_id: str
    granted_kw: float
    obligation_id: str | None = None
    hub_id: str | None = None
    is_headroom: bool = False
    reason_code: str = ""


@dataclass(frozen=True, slots=True)
class ShortfallReport:
    """A shortfall against an obligation, never a silent reallocation (K13)."""

    obligation_id: str
    bank_id: str
    shortfall_kw: float
    reason_code: str


@dataclass(frozen=True, slots=True)
class SubstitutionEvent:
    obligation_id: str
    bank_id: str
    from_hub_ids: tuple[str, ...]
    to_hub_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CycleResult:
    """The allocator's S1-S7 output for one cycle: a proposed batch (grants + reasons), any
    shortfalls (each pinned to its own obligation), and substitution events for the trace.
    """

    cycle_id: str
    grants: tuple[ProposedGrant, ...] = field(default_factory=tuple)
    shortfalls: tuple[ShortfallReport, ...] = field(default_factory=tuple)
    substitutions: tuple[SubstitutionEvent, ...] = field(default_factory=tuple)
