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

from opengrid.core.models.engine import ServiceType
from opengrid.core.physics import DEFAULT_ETA_C, DEFAULT_ETA_D

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
    # CORE-003/K1: SoC/reserve/capacity/efficiency, live from the fleet twin's telemetry (engine's
    # `EngineFleetGateway.fleet_state`), so the allocator's own capability path can cap
    # `free_discharge_kw` by `hub_sustainable_discharge_kw` over the command's HOLD HORIZON (the lease
    # TTL, not just the 2 s tick) -- `hub_capability()`'s reserve-safe cap alone still lets a sliver of
    # energy just above reserve be offered at the hub's full power rating for the whole lease duration.
    # `None` means the fleet twin has no live/fresh SoC for this hub this cycle -- K7's conservative
    # fallback is 0 kW discharge capability, NEVER trusting `free_discharge_kw` at face value (a stale
    # or missing SoC reading must never silently imply full power is safe).
    soc_kwh: float | None = None
    reserve_kwh: float | None = None
    e_kwh: float | None = None  # usable energy capacity, for the symmetric charge-side cap
    eta_c: float = DEFAULT_ETA_C
    eta_d: float = DEFAULT_ETA_D
    # Nameplate discharge kW regardless of health/SoC, so a shortfall can be attributed to the K13
    # exception that caused it (device fault = L0, reserve floor = L1). `None`: unknown, attributes nothing.
    rated_kw: float | None = None
    # Measured power now (+charge/-discharge), for the PQ phase-balance weighting (S5.2 step 2).
    p_kw: float | None = None
    # 09 S1.9 flow-limit inputs (F1/F2/F3). `None` = not reported: the static limits apply, never a guess.
    cell_temp_c: float | None = None
    p_dis_max_kw: float | None = None  # the hub BMS's own discharge limit
    meter_kw: float | None = None  # net import at the home meter (+ import, - export)
    export_limit_kw: float | None = None  # interconnection export limit at the meter
    xfmr_id: str | None = None  # service transformer the home hangs off

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
    feeder_id: str | None = None  # 09 F3 per-cycle feeder budget
    substation_id: str | None = None  # 09 F3 per-cycle substation budget
    #: K15 territory (`opengrid.market.territory_of_zone`): a regulated utility id, `ERCOT_COMPETITIVE`, or
    #: `None` = unknown (fail closed when territory is enforced).
    territory: str | None = None
    #: K15(b): the territory's utility grants wholesale (FREE) access. Ignored for competitive-area banks.
    free_access: bool = False


@dataclass(frozen=True, slots=True)
class ObligationCall:
    """A committed obligation's demand on one bank this cycle (02a S5.1's `active_calls`).

    `committed_kw` is the frozen K13 floor (`commitment.committed_kw`); the allocator never proposes
    a grant below `min(committed_kw, prior_granted_kw)` without an override reason code.
    """

    obligation_id: str
    bank_id: str
    service_type: ServiceType
    tier: Tier
    committed_kw: float
    eligible_hub_ids: tuple[str, ...]
    prior_granted_kw: float | None = None
    value_per_mwh: float = 0.0
    #: Already escalated to SHORTFALL mid-window: still dispatched best-effort (owner decision 2026-09-26),
    #: its partial grants carry the shortfall code G-19 corroborates.
    in_shortfall: bool = False
    #: ERCOT_AS only: ERCOT has deployed this award right now (`og.as_deployment`). An AS award is a
    #: CAPACITY HOLD: undeployed it is granted 0 kW discharge with its reservation kept (K13), and
    #: discharges up to `committed_kw` only while deployed.
    as_deployed: bool = False

    @property
    def is_as_hold(self) -> bool:
        """An ERCOT_AS capacity hold not currently deployed: 0 kW, capacity and energy stay locked."""
        return self.service_type == "ERCOT_AS" and not self.as_deployed


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
    #: The bank's own headroom-discharge threshold: the replacement cost of the energy it would spend
    #: (cheapest recharge price ahead + the M1 delivery charge, over round-trip efficiency; 09 S1.8 G9).
    #: None: the cycle's fixed default threshold.
    threshold_usd_per_mwh: float | None = None


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
    #: K7 escalation (og.scope_posture CONSERVATIVE, written by the guardian): banks that get NO new
    #: uncommitted/market dispatch (headroom export); committed obligations continue under K13 best effort.
    conservative_bank_ids: frozenset[str] = frozenset()


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
    #: ERCOT_AS obligations held this cycle (undeployed capacity hold: 0 kW, reservation kept).
    held: tuple[str, ...] = field(default_factory=tuple)
