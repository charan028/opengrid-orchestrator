"""Pure-logic data shapes for the allocator's 2-second cycle (02a S5).

These are plain, hashable dataclasses -- not the pydantic `og.*` row models in
`opengrid.core.models.engine` -- because the cycle's hot path runs over thousands of hubs every 2 s
and must stay allocation-light (numpy-friendly floats, no DB/ORM objects). The thin adapter in
`opengrid.allocator.__init__` is the only place that converts between these and the `Grant` row model.

Units: power in kW, apparent power in kVA, money in $/MWh, time in seconds unless named otherwise.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from opengrid.core.models.engine import ServiceType
from opengrid.core.models.market import Territory
from opengrid.core.physics import DEFAULT_ETA_C, DEFAULT_ETA_D
from opengrid.market.territory import FREE, MarketRef

if TYPE_CHECKING:
    from opengrid.allocator.pq_eligibility import EligibilityResult

#: Services held at 0 kW until called (`og.as_deployment` active for the obligation), then delivered up to
#: `committed_kw`: ERCOT_AS (NPRR1282) and the utility toll (D-29: REGULATED_CAPACITY, variant TOLLING).
HOLD_SERVICE_TYPES: frozenset[str] = frozenset({"ERCOT_AS", "REGULATED_CAPACITY"})

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
    units: int | None = None  # battery/inverter units in the home (og.hub.units, migration 0032)
    #: A utility-scale asset (og.asset SUBSTATION on this bank, e.g. the D-29 20 MW set): rated at its
    #: nameplate, never the home per-unit cap (`core.limits.continuous_power_kw`).
    utility_scale: bool = False

    @property
    def is_healthy(self) -> bool:
        return self.health == "OK"

    def evolve(self, **changes: Any) -> HubSnapshot:
        """`dataclasses.replace` for the 2 s hot path: the same copy-with-changes, without re-running
        field introspection and the frozen `__init__` (several times cheaper per call at 2,000 hubs)."""
        new = object.__new__(HubSnapshot)
        for name in _HUB_SLOTS:
            object.__setattr__(new, name, changes[name] if name in changes else getattr(self, name))
        return new


_HUB_SLOTS: tuple[str, ...] = HubSnapshot.__slots__


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
    territory: Territory | None = None
    #: K15(b): the territory's utility grants wholesale (FREE) access. Ignored for competitive-area banks.
    free_access: bool = False
    #: D-37 (`og.bank.availability`, migration 0046): False = UNAVAILABLE (regulated, no contract). The
    #: allocator dispatches nothing on it (no headroom, no obligation) except K13-grandfathered calls.
    available: bool = True


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
    #: ERCOT_AS only: the product's full-deployment duration (NSPIN 4 h, ECRS 1 h; `product_rule.
    #: duration_minutes`). `None`: `energy_hold.DEFAULT_AS_DEPLOYMENT_H`.
    hold_duration_h: float | None = None
    #: While called: hours left of the active deployment (`og.as_deployment.end_at - now`). The energy hold
    #: then covers only the rest of this call, not a fresh full duration (review R3).
    deployment_remaining_h: float | None = None
    #: K15: the obligation's market (its contract's `market`/`utility_id`). `None` = unknown or
    #: inconsistent market data: never served while territory is enforced (fail closed).
    market_ref: MarketRef | None = FREE
    #: D-37 / K13: committed before its (now UNAVAILABLE, regulated) bank switched, and still holding its
    #: reservation there (`opengrid.market.availability.GRANDFATHERED_SQL`): it completes untouched, exempt
    #: from the availability block and from the K15 territory check.
    grandfathered: bool = False

    @property
    def is_capacity_hold(self) -> bool:
        """A capacity-hold service (`HOLD_SERVICE_TYPES`): its capacity and a full call's energy stay held."""
        return self.service_type in HOLD_SERVICE_TYPES

    @property
    def is_as_hold(self) -> bool:
        """A capacity hold not currently called (ERCOT_AS undeployed; D-29: a utility toll not called):
        0 kW, capacity and energy stay locked."""
        return self.is_capacity_hold and not self.as_deployed


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
    #: 09 S1.8 e^hold: the selector plan's hard SoC floor per bank (kWh), where published. Headroom never
    #: discharges below it (nor below the AS energy hold).
    hold_floor_kwh: Mapping[str, float] = field(default_factory=dict)


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
    #: Per-hub realization of PQ-sensitive obligations (the engine builds their hub items from these, so
    #: only PQ-eligible hubs deliver them).
    hub_allocations: tuple[HubAllocation, ...] = field(default_factory=tuple)
    #: PQ eligibility left an obligation less capability than it needs this cycle (trace PQ_CAPABILITY_REDUCED).
    pq_reductions: tuple[PqCapabilityReduction, ...] = field(default_factory=tuple)
    #: Obligations a K15 territory check kept off a bank this cycle.
    territory_blocks: tuple[TerritoryBlock, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class HubAllocation:
    """One obligation's water-filled per-hub kW on one bank this cycle."""

    obligation_id: str
    bank_id: str
    per_hub_kw: tuple[tuple[str, float], ...]


@dataclass(frozen=True, slots=True)
class PqCapabilityReduction:
    """PQ eligibility (S5.2) shrank what an obligation's hubs can deliver below what it needs this cycle."""

    obligation_id: str
    bank_id: str
    needed_kw: float
    capability_before_kw: float
    capability_after_kw: float
    excluded_hub_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TerritoryBlock:
    """K15: an obligation (or the bank's FREE headroom, `obligation_id=None`) not served on a bank."""

    bank_id: str
    obligation_id: str | None
    reason_code: str


@dataclass(frozen=True, slots=True)
class PqDispatchContext:
    """S5.2/S5.4 real-time PQ inputs for one cycle.

    - `results_by_service`: the S5.2 eligibility verdicts per PQ-sensitive service type (hubs not listed
      are ineligible for it);
    - `phase_by_hub_id`: each hub's phase connection, for the phase-balance weight;
    - `excluded_by_obligation`: hubs the S5.4 ladder took off an obligation (SUBSTITUTE_HUBS/EXCLUDE_HUB);
      they are passed to substitution as unhealthy, so the swap is recorded with R-SUBSTITUTION."""

    results_by_service: Mapping[str, EligibilityResult] = field(default_factory=dict)
    phase_by_hub_id: Mapping[str, str] = field(default_factory=dict)
    excluded_by_obligation: Mapping[str, frozenset[str]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FlowLimits:
    """09 S1.9 dispatch-side flow limits (F1-F3). Each applies only where its data exists; missing data
    keeps the static limits (rated power, bank kVA) -- never an optimistic guess.

    - F1: `P_max(SoC, T)` derating, capped by the hub's BMS limit when reported;
    - F2: home load first, then the meter export limit (`default_export_limit_kw` when a hub reports none);
    - F3: service-transformer group caps (`xfmr_kva`), per-cycle feeder and substation discharge budgets
      (`feeder_budget_kw`/`substation_budget_kw`: the static ratings, or rating minus measured flow)."""

    enabled: bool = False
    default_export_limit_kw: float | None = None
    xfmr_kva: Mapping[str, float] = field(default_factory=dict)
    feeder_budget_kw: Mapping[str, float] = field(default_factory=dict)
    substation_budget_kw: Mapping[str, float] = field(default_factory=dict)
    #: Registry topology (migration 0029) for snapshots that do not carry it: hub -> service transformer,
    #: hub -> meter export limit, bank -> feeder, bank -> substation.
    xfmr_by_hub: Mapping[str, str] = field(default_factory=dict)
    export_limit_by_hub: Mapping[str, float] = field(default_factory=dict)
    feeder_by_bank: Mapping[str, str] = field(default_factory=dict)
    substation_by_bank: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CycleExtras:
    """Optional per-cycle inputs beyond S1-S7's core (all default off): closed-loop caps (S4.a/b), PQ
    context (S5.2/S5.4), K15 territory enforcement, and flow limits (09 S1.9)."""

    closed_loop_caps: Mapping[tuple[str, str], float] = field(default_factory=dict)
    pq: PqDispatchContext | None = None
    enforce_territory: bool = False
    flow_limits: FlowLimits = field(default_factory=FlowLimits)
    #: K4 fail-safe: hubs kept out after a guardian item veto (R-HUB-VETO-EXCLUDED); unavailable this cycle.
    excluded_hub_ids: frozenset[str] = frozenset()
    #: The subset held by a live operator target (R-OPERATOR-OVERRIDE on shortfalls there).
    operator_hub_ids: frozenset[str] = frozenset()
