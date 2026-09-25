"""Pure data types for the Mode O selector model (02a S3.2, S3.6). No I/O.

`model.py` consumes these to build a highspy model; `solve.py`/`extract.py` produce the typed results;
`validate.py`/`rule_fallback.py` consume the same types so all four pieces share one vocabulary.

Units: kW for power, $/MWh for prices (divided by 1000 to match the objective's $/kWh terms, 02a S3.4),
$/kWh for degradation cost, minutes for `interval_minutes`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from opengrid.core.products import VariableKind

ScenarioName = Literal["P10", "P50", "P90"]
GateKind = Literal["SCHEDULED_15MIN", "ADMISSION", "RENOMINATION"]


@dataclass(frozen=True, slots=True)
class BankSnapshot:
    """A bank's discharge capability per interval index (from `fleet.capability`, 02b S4)."""

    bank_id: str
    max_discharge_kw: dict[int, float]


@dataclass(frozen=True, slots=True)
class ScenarioPrice:
    """One P10/P50/P90 price path (from `forecast.scenarios`, 02b S3). Bank-independent for MVP-S
    (one ERCOT load-zone price feeds every bank's energy value, 02a S3.2 v^E_{b,t,omega})."""

    scenario: ScenarioName
    probability: float
    price_usd_per_mwh: dict[int, float]


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


@dataclass(frozen=True, slots=True)
class CandidateOpportunity:
    """An `OFFERED` opportunity competing for uncommitted headroom (02a S1.4, S3.2 O^new).

    `requested_kw` is a single scalar per 02a S1.4's schema (one number for the whole window) -- the
    model treats a selected candidate as a flat profile over `window_intervals`, which is exactly what
    the `opportunity` row encodes, not a simplification of it.
    """

    opportunity_id: str
    contract_id: str
    eligible_bank_ids: tuple[str, ...]
    window_intervals: tuple[int, ...]
    requested_kw: float
    value_per_mwh: float
    variable_kind: VariableKind
    min_qty_kw: float
    increment_kw: float
    degradation_cost_per_kwh: float = 0.03
    tier: str = "T4"
    category: Literal["FIRM", "AS", "MARKET"] = "MARKET"
    """Rule-fallback (F2) priority bucket (02a S3.7 "firm first, then AS, then market"). `HOME`,
    `DIST_DEFERRAL` and `PARTNER_CAPACITY` opportunities are `FIRM`; `ERCOT_AS` is `AS`; `ERCOT_ENERGY`
    (spot-like) is `MARKET`. Independent of `variable_kind`, which governs the LP/MILP's variable shape."""


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

    @property
    def interval_hours(self) -> float:
        return self.interval_minutes / 60.0


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


@dataclass(frozen=True, slots=True)
class ValidationResult:
    ok: bool
    violations: tuple[str, ...] = ()
