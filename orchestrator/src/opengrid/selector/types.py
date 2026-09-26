"""Pure data types for the Mode O selector model (02a S3.2, S3.6). No I/O.

`model.py` consumes these to build a highspy model; `solve.py`/`extract.py` produce the typed results;
`validate.py`/`rule_fallback.py` consume the same types so all four pieces share one vocabulary.

Units: kW for power, $/MWh for prices (divided by 1000 to match the objective's $/kWh terms, 02a S3.4),
$/kWh for degradation cost, minutes for `interval_minutes`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from opengrid.core.physics import DEFAULT_ETA_C, DEFAULT_ETA_D, DEFAULT_SELF_DISCHARGE_KWH_PER_H
from opengrid.core.products import VariableKind

ScenarioName = Literal["P10", "P50", "P90"]
GateKind = Literal["SCHEDULED_15MIN", "ADMISSION", "RENOMINATION"]


@dataclass(frozen=True, slots=True)
class BankSnapshot:
    """A bank's capability per interval index (from `fleet.capability`, 02b S4), plus the energy
    envelope the SoC balance (02a S3.2/S3.3 C1/C2/C15) needs.

    `max_charge_kw`/`capacity_kwh`/`reserve_kwh`/`initial_soc_kwh` are additive to the pre-existing
    `max_discharge_kw`-only snapshot: `capacity_kwh <= 0` (the default) means "no energy envelope
    supplied for this bank" and `build_mode_o_model` skips SoC modeling for it entirely, so every
    existing caller/test that only ever set `max_discharge_kw` keeps its prior (power-only) behavior.
    `eta_c`/`eta_d`/`self_discharge_kwh_per_h` default to `opengrid.core.physics`'s MVP-S constants
    (02a S3.2's eta_c=eta_d=0.9487) -- the selector never re-derives these, only imports them, per
    BUILD.md S1's no-duplicated-functions rule.
    """

    bank_id: str
    max_discharge_kw: dict[int, float]
    max_charge_kw: dict[int, float] = field(default_factory=dict)
    initial_soc_kwh: float = 0.0
    capacity_kwh: float = 0.0
    reserve_kwh: float = 0.0
    eta_c: float = DEFAULT_ETA_C
    eta_d: float = DEFAULT_ETA_D
    self_discharge_kwh_per_h: float = DEFAULT_SELF_DISCHARGE_KWH_PER_H
    zone: str | None = None
    """ERCOT settlement (load) zone, e.g. `LZ_NORTH` or `LZ_AEN`. Informational in the model (prices are
    already resolved per bank in `ScenarioPrice.price_by_bank`)."""
    territory: str | None = None
    """K15 territory (`opengrid.market.territory.territory_of_zone`): a regulated utility id or
    `ERCOT_COMPETITIVE`. `None` = not resolved (hand-built inputs; the gate always resolves it)."""
    wear_usd_per_kwh: float = 0.0
    """09 D8 (Frank #7): wear per AC kWh actually discharged, at this bank's asset-class rate, for EVERY
    purpose (obligation delivery and free headroom alike) -- never on holds or charging. Priced through
    `opengrid.core.economics.wear_cost`. 0 = no wear (hand-built inputs); the gate sets the class rate."""
    delivery_charge_usd_per_kwh: float = 0.0
    """09 D5 (M1): TDSP volumetric delivery charge per kWh drawn from the grid to charge, ERCOT
    competitive area only (`opengrid.market.charging.free_charging_cost`). 0 in a regulated territory."""
    charge_price_usd_per_kwh: dict[int, float] = field(default_factory=dict)
    """Regulated-territory charging price per interval (09 C25(d): the utility's own charging terms,
    `opengrid.market.charging.regulated_charging_cost`, solar share blended in). Non-empty REPLACES the
    wholesale zone price for charging; empty = charge at the scenario's zone price + M1."""
    free_market_access: bool = True
    """K15(b): may this bank take FREE (ERCOT) headroom? False inside a regulated territory whose
    utility has not granted wholesale access (09 D2, default no)."""
    solar_charge_kw: dict[int, float] = field(default_factory=dict)
    """09 C27 S^sol: solar power available to charge this bank per interval (kW). Empty = no solar source:
    every charged kWh is grid-drawn."""
    solar_cost_usd_per_kwh: float | None = None
    """Cost of a solar-charged kWh: the utility's contract solar price in a regulated territory; None =
    behind-the-meter PV surplus valued at its forgone export credit (the zone price), never paying M1."""
    solar_share_floor: float = 0.0
    """09 D4 / D-22: at least this share of the bank's territory's charged energy is solar (soft floor,
    priced slack). 0.30 in a regulated territory; 0 elsewhere."""
    solar_share: dict[int, float] = field(default_factory=dict)
    """D-28 measured solar share of charging per interval (`solar_charge_kw` = share x charge envelope)."""
    solar_share_source: dict[int, str] = field(default_factory=dict)
    """Which D-28 source each interval's share came from: TELEMETRY, ERCOT_SOLAR or ASSUMPTION."""
    grid_charge_intervals: frozenset[int] | None = None
    """Intervals in which grid charging is allowed; None = any. Owner decision 2026-09-26 (D-22): a
    regulated bank charges from the grid only at its utility's night / off-peak rate."""

    def grid_charge_allowed(self, t: int) -> bool:
        return self.grid_charge_intervals is None or t in self.grid_charge_intervals

    @property
    def models_soc(self) -> bool:
        """Whether this bank carries an energy envelope the SoC balance should enforce."""
        return self.capacity_kwh > 0.0

    def charge_cost_usd_per_kwh(self, wholesale_usd_per_mwh: float, t: int) -> float:
        """What one grid-drawn kWh charged at interval `t` costs this bank (09 S1.5 `c^ch`): the
        regulated tariff when set, else the zone's wholesale price plus M1."""
        regulated = self.charge_price_usd_per_kwh.get(t)
        if regulated is not None:
            return regulated
        return wholesale_usd_per_mwh / 1000.0 + self.delivery_charge_usd_per_kwh


@dataclass(frozen=True, slots=True)
class ScenarioPrice:
    """One P10/P50/P90 price path (from `forecast.scenarios`, 02b S3). `price_usd_per_mwh` is the fleet
    path (mean of the load zones); `price_by_bank` holds each bank's OWN load-zone path (02a S3.2's
    v^E_{b,t,omega} is per bank). Architect finding (a): every bank was priced at whichever zone's row
    came last (LZ_WEST), and load-forecast rows were folded into the price path."""

    scenario: ScenarioName
    probability: float
    price_usd_per_mwh: dict[int, float]
    price_by_bank: dict[str, dict[int, float]] = field(default_factory=dict)

    def price_at(self, bank_id: str, t: int) -> float:
        """Bank `bank_id`'s $/MWh at interval `t`: its zone's path, else the fleet path, else 0."""
        by_t = self.price_by_bank.get(bank_id)
        if by_t is not None and t in by_t:
            return by_t[t]
        return self.price_usd_per_mwh.get(t, 0.0)


@dataclass(frozen=True, slots=True)
class CommittedObligation:
    """A `COMMITTED`/`DELIVERING` obligation's frozen delivery profile (02a S2.2, S3.3 C24).

    `committed_kw_by_interval` is the equality-freeze parameter ŷ_{o,t}: an obligation-level total,
    not tied to one bank (the `commitment` table carries no `bank_id`, 02a S1.6 -- bank realization is
    a `reservation`/`grant` concern). The model may spread it across `eligible_bank_ids` freely; that is
    bank *substitution*, always allowed (02a S1's "substitution of homes... is always allowed and is not
    an interrupt" extended here to banks for MVP-S's bank-granularity selector), never a reduction.
    """

    obligation_id: str
    eligible_bank_ids: tuple[str, ...]
    committed_kw_by_interval: dict[int, float]
    degradation_cost_per_kwh: float = 0.03
    """As `CandidateOpportunity.degradation_cost_per_kwh`: the contract's rate, not used by the LP."""
    energy_hold_h: float = 0.0
    """ERCOT_AS capacity hold (NPRR1282): > 0 means the award does NOT drain the bank each interval;
    instead the bank must keep `kW x energy_hold_h / eta_d` above its reserve floor (Non-Spin 4 h, ECRS
    1 h) while the award is held. 0 = an ordinary delivery that discharges its profile."""
    value_per_mwh: float = 0.0
    """The obligation's own contract value ($/MWh), for the forgone-upside figure (KPI-22) only."""
    market: Literal["REGULATED", "FREE"] = "FREE"
    utility_id: str | None = None
    expected_deployment_share: float = 0.0
    """09 S1.3 psi: expected fraction of a held award actually deployed (discharged). Wear (D8) is
    charged on that expected discharge; the hold itself pays none."""


@dataclass(frozen=True, slots=True)
class CandidateOpportunity:
    """An `OFFERED` opportunity competing for uncommitted headroom (02a S1.4, S3.2 O^new).

    `requested_kw` is a single scalar per 02a S1.4's schema (one number for the whole window) -- the
    model treats a selected candidate as a flat profile over `window_intervals`, which is exactly what
    the `opportunity` row encodes, not a simplification of it.
    """

    opportunity_id: str
    obligation_id: str
    contract_id: str
    eligible_bank_ids: tuple[str, ...]
    window_intervals: tuple[int, ...]
    requested_kw: float
    value_per_mwh: float
    variable_kind: VariableKind
    min_qty_kw: float
    increment_kw: float
    degradation_cost_per_kwh: float = 0.03
    """The contract's `degradation_cost` (settle's rate). The LP does NOT use it: wear is charged at the
    discharging bank's asset-class rate (`BankSnapshot.wear_usd_per_kwh`, 09 D8)."""
    tier: str = "T4"
    category: Literal["FIRM", "AS", "MARKET"] = "MARKET"
    """Rule-fallback (F2) priority bucket (02a S3.7 "firm first, then AS, then market"). `HOME`,
    `DIST_DEFERRAL` and `PARTNER_CAPACITY` opportunities are `FIRM`; `ERCOT_AS` is `AS`; `ERCOT_ENERGY`
    (spot-like) is `MARKET`. Independent of `variable_kind`, which governs the LP/MILP's variable shape."""
    service_type: str = ""
    """The contract's service type (e.g. DATA_CENTER): PQ-sensitive profiles are capped at their
    PQ-eligible capacity at commit time (WP-D)."""
    energy_hold_h: float = 0.0
    """As `CommittedObligation.energy_hold_h`: an ERCOT_AS candidate is selectable only where the bank
    can hold `kW x energy_hold_h / eta_d` above reserve; it never drains SoC while held."""
    market: Literal["REGULATED", "FREE"] = "FREE"
    """09 D1: the contract's market (`opengrid.market.territory.market_of`). REGULATED candidates are
    selected in stage R, before any FREE value is considered (09 D3, lexicographic)."""
    utility_id: str | None = None
    expected_deployment_share: float = 0.0
    """As `CommittedObligation.expected_deployment_share`."""

    @property
    def is_regulated(self) -> bool:
        return self.market == "REGULATED"


@dataclass(frozen=True, slots=True)
class ModelInputs:
    """Everything `build_mode_o_model` needs -- the exact tuple named in the build brief:
    (banks, forecast scenarios, obligations committed + candidate, product rules [folded into
    `CandidateOpportunity.variable_kind`/`min_qty_kw`/`increment_kw`], prices [in `scenarios`])."""

    intervals: tuple[int, ...]
    interval_minutes: float
    banks: tuple[BankSnapshot, ...]
    scenarios: tuple[ScenarioPrice, ...]
    committed: tuple[CommittedObligation, ...]
    candidates: tuple[CandidateOpportunity, ...]
    terminal_soc_slack_kwh: float = 0.0
    """02a S3.3 C15 (simplified terminal energy): a bank's SoC at the end of the horizon must be >=
    its `initial_soc_kwh` minus this slack, for every scenario. 0.0 (the default) requires the bank to
    end the horizon at least as full as it started (no free depletion); a nonzero slack allows a
    configurable planned net drawdown."""
    lexicographic_tolerance: float = 0.001
    """09 D3 epsilon^lex: stage F may give up at most this fraction of the stage-R (regulated) optimum."""

    @property
    def interval_hours(self) -> float:
        return self.interval_minutes / 60.0

    @property
    def has_regulated_candidates(self) -> bool:
        return any(c.is_regulated for c in self.candidates)

    def energy_hold_hours(self) -> dict[str, float]:
        """ybar key (committed `obligation_id` / candidate `opportunity_id`) -> energy-hold hours: the
        ERCOT_AS capacity holds that lock kW and stored energy instead of draining SoC (model + validate
        must agree on exactly this set)."""
        holds = {co.obligation_id: co.energy_hold_h for co in self.committed if co.energy_hold_h > 0}
        holds.update({c.opportunity_id: c.energy_hold_h for c in self.candidates if c.energy_hold_h > 0})
        return holds

    def expected_deployment_shares(self) -> dict[str, float]:
        """ybar key -> psi, for the held awards with an expected deployment (09 S1.3)."""
        held = self.energy_hold_hours()
        shares = {
            co.obligation_id: co.expected_deployment_share
            for co in self.committed
            if co.obligation_id in held and co.expected_deployment_share > 0
        }
        shares.update(
            {
                c.opportunity_id: c.expected_deployment_share
                for c in self.candidates
                if c.opportunity_id in held and c.expected_deployment_share > 0
            }
        )
        return shares

    def regulated_obligation_ids(self) -> set[str]:
        """ybar keys of REGULATED obligations (committed and candidate): their delivery windows bar
        charging on the banks serving them (09 C7(b)', D13 no wash trade)."""
        ids = {co.obligation_id for co in self.committed if co.market == "REGULATED"}
        ids.update(c.opportunity_id for c in self.candidates if c.is_regulated)
        return ids


@dataclass(frozen=True, slots=True)
class SolverSettings:
    """HiGHS run settings for one gate (02a S3.7)."""

    mip_rel_gap: float
    time_limit_s: float
    accept_gap_at_limit: float = 0.05


def solver_settings_for(gate_kind: GateKind) -> SolverSettings:
    """02a S3.7's table. `ADMISSION`/`RENOMINATION` share the tightest, single-obligation-scope budget."""
    if gate_kind == "SCHEDULED_15MIN":
        return SolverSettings(mip_rel_gap=0.01, time_limit_s=45.0)
    return SolverSettings(mip_rel_gap=0.01, time_limit_s=15.0)


SolverStatus = Literal["OPTIMAL", "TIME_LIMIT_GAP", "INFEASIBLE_F1", "INFEASIBLE_F2", "RULE_FALLBACK"]


@dataclass(frozen=True, slots=True)
class ExtractedPlan:
    """The selector's output for one gate (02a S3.8, S6.10).

    `selected_x`/`selected_q`: the chosen indicator/quantity per candidate opportunity_id.
    `committed_profile`: the (possibly bank-redistributed) ŷ per obligation_id/interval, always
    honoring `CommittedObligation.committed_kw_by_interval`'s totals (K13).
    `bank_interval_allocation`: ybar_{o,b,t} for every (obligation_id, bank_id, interval).
    `headroom_schedule`: h_{b,t,omega} -- the free-headroom spot energy schedule.
    `bank_capacity_duals`: shadow price of each bank/interval capacity row (the "price of firmness":
    the $/kW value the model would pay for one more kW of bank headroom at that interval).
    """

    solver_status: SolverStatus
    plan_mode: Literal["L-DA", "L-ID", "RULE_FALLBACK"]
    objective_value: float
    solver_gap: float | None
    solver_time_ms: int
    selected_x: dict[str, bool]
    selected_q: dict[str, float]
    bank_interval_allocation: dict[tuple[str, str, int], float]
    committed_profile: dict[str, dict[int, float]]
    headroom_schedule: dict[tuple[str, int, str], float]
    bank_capacity_duals: dict[tuple[str, int], float]
    soc_by_bank_interval_scenario: dict[tuple[str, int, str], float] = field(default_factory=dict)
    """SoC (kWh) at the *start* of each interval, per bank/scenario, for every bank with
    `BankSnapshot.models_soc`; interval index `max(t)+1` is the terminal SoC (02a S3.3 C15)."""
    charge_by_bank_interval_scenario: dict[tuple[str, int, str], float] = field(default_factory=dict)
    """g^grid_{b,t,omega}: GRID charge power (kW) per bank/interval/scenario, for every bank with
    `BankSnapshot.models_soc`."""
    solar_charge_by_bank_interval_scenario: dict[tuple[str, int, str], float] = field(default_factory=dict)
    """g^sol_{b,t,omega}: solar charge power (kW), for banks with a solar source (09 C27)."""
    stored_energy_value: dict[tuple[str, int], float] = field(default_factory=dict)
    """09 D7 water value nu_{b,t}: $ per kWh (DC, stored) held at the END of interval t, the
    probability-weighted dual of the C1 SoC balance row. Empty when duals are unavailable (rule plan,
    or the fixed-integer LP re-solve did not finish)."""
    stage_r_objective: float | None = None
    """09 D3 stage-R (regulated) optimum; None when the gate had no regulated candidate."""
    shadow: ShadowComparison | None = None
    """ES05-S07: the rule fallback (F2) run alongside the LP on the same inputs, with both net values."""


@dataclass(frozen=True, slots=True)
class PlanValue:
    """Expected net value of a plan over the scenarios, evaluated by ONE function
    (`selector.value.plan_net_value`) for the LP and the rule baseline alike, so the value-added figure
    compares like with like. All in $, positive magnitudes for costs."""

    obligation_revenue: float
    energy_revenue: float
    charging_energy_cost: float
    delivery_charge: float
    wear: float
    terminal_energy_value: float
    """Change in stored energy over the horizon, valued at the bank's average charging cost (so a plan
    that ends emptier is not credited with energy it would have to buy back)."""

    @property
    def net(self) -> float:
        return (
            self.obligation_revenue
            + self.energy_revenue
            - self.charging_energy_cost
            - self.delivery_charge
            - self.wear
            + self.terminal_energy_value
        )


@dataclass(frozen=True, slots=True)
class ShadowObligationInterval:
    """One obligation-interval of the shadow comparison: what the LP and the rule each gave it, and the
    best competing candidate value for a committed obligation's locked capacity (settle's forgone
    upside input, `pnl.forgone_upside`)."""

    obligation_id: str
    interval: int
    lp_kw: float
    rule_kw: float
    best_competing_value_per_kwh: float | None = None


@dataclass(frozen=True, slots=True)
class ShadowComparison:
    """ES05-S07 / KPI-22: LP vs rule baseline on the same gate inputs."""

    lp_value: PlanValue
    rule_value: PlanValue
    rule_plan: ExtractedPlan
    forgone_upside: float
    obligation_intervals: tuple[ShadowObligationInterval, ...] = ()

    @property
    def value_added(self) -> float:
        return self.lp_value.net - self.rule_value.net


@dataclass(frozen=True, slots=True)
class ValidationResult:
    ok: bool
    violations: tuple[str, ...] = ()
