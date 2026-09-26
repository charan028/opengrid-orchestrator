"""Mode O model builder (02a S3.2-S3.7 subset). Pure function: `ModelInputs` -> a highspy `Highs` model.

No solving, no I/O -- `solve.py` runs it, `extract.py` reads the solution back into `ExtractedPlan`.

**Deliverable-boundary simplification (documented, not hacked around).** `fleet.capability` still
reduces hub/bank *instantaneous power* state to a discharge/charge-power envelope per bank/interval
before the selector ever sees it (02b S12 "fleet never simulates -- it only stores and aggregates") --
the selector never re-derives per-hub physics. But a day-ahead plan built only from that per-interval
power envelope, with no memory of *energy* across intervals, can commit more energy over a window than
the bank's SoC can actually deliver (a correctness bug fixed here): this builder now also carries the
per-bank *energy* envelope (`BankSnapshot.capacity_kwh`/`reserve_kwh`/`initial_soc_kwh`, populated once
`fleet` exposes them -- see the module's final-report note) through C1's energy balance, C2's SOC
bounds (enforced as the SoC variables' own bounds) and C15's simplified terminal-energy floor, using
`core.physics`'s shared eta_c/eta_d/self-discharge constants (never re-deriving the formula's
coefficients, only inlining its already-linear affine form -- see the SoC section below for why). This
builder implements the constraint families that are genuinely the selector's to decide: C1 (bank energy
balance across intervals, plus the aggregate power balance reduced to the discharge envelope), C2 (SOC
bounds), C6/C13 (one obligation-set per bank/interval, i.e. K2 one-buyer, materialized as a
shared-capacity row), C12 (firm delivery + locality via eligible-bank sets), C15 (terminal energy,
simplified per 02a S3.3), C16 (non-anticipativity, structural: first-stage variables carry no scenario
index), and C24 (the commitment lock, an equality on the frozen total). Charge-side constraints C7-C9,
charge/discharge exclusivity C14, cycle budget C18, C3/C11 (AS-vs-firm exclusivity beyond
shared-capacity competition) and C17 (reserve-deficit recovery) are still deferred; see the module's
final-report note.

Variables:
    x_o        in {0,1}         -- BINARY candidates (all-or-nothing).
    q_o        in [0, max_kw]   -- CONTINUOUS candidates.
    u_o, n_o   binary, integer  -- SEMI_CONTINUOUS enable flag and step count; q_o derived from both.
    ybar[o,b,t] >= 0            -- first-stage bank/interval allocation for obligation o (committed and
                                    candidate obligations share this variable family; C24 pins the
                                    committed ones via an equality on their per-interval total, S3.2's
                                    "ŷ_{o,b,t} := commitment.committed_kw, not a decision the solver can
                                    move" is honored at the *total* level, per `types.CommittedObligation`'s
                                    docstring on bank substitution being allowed).
    h[b,t,w]   >= 0              -- second-stage free-headroom spot energy schedule, scenario-indexed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import highspy
import numpy as np

from opengrid.selector.types import BankSnapshot, ModelInputs
from opengrid.selector.value import wear_usd_per_kwh

_EPS = 1e-9
#: C15 terminal-energy shortfall cost ($/kWh below the next-day floor, i.e. $1,000/MWh): above every
#: MVP-S energy/capacity value, so energy is only drawn below the floor when a hard constraint needs it.
TERMINAL_SHORTFALL_PENALTY_USD_PER_KWH = 1.0
#: An ERCOT_AS energy hold keeps this fraction of the bank's capacity above reserve too: the guardian's
#: G-01-ENERGY floor at lease end (else the last leases of a full deployment are vetoed).
AS_HOLD_FLOOR_FRACTION = 0.01
_MIN_MEANINGFUL_KW = 1e-6  # below this, treat capacity as exactly 0 -- avoids HiGHS "tiny coefficient"
# numerical errors on pathologically small (but nonzero) capacity readings.


def _wash_pays(bank: BankSnapshot, price_usd_per_mwh: float, t: int) -> bool:
    """Whether buying one grid kWh and selling what it stores in the same interval would pay or tie:
    eta_c * eta_d * (price - wear) >= the grid charging cost. Only at deeply negative prices (or with no
    losses, wear or M1 at all) -- elsewhere the LP nets the two and no wash guard is needed."""
    round_trip = bank.eta_c * bank.eta_d
    sale = round_trip * (price_usd_per_mwh / 1000.0 - wear_usd_per_kwh(bank))
    return sale >= bank.charge_cost_usd_per_kwh(price_usd_per_mwh, t) - _EPS


def _clamped(kw: float) -> float:
    return 0.0 if kw < _MIN_MEANINGFUL_KW else kw


@dataclass
class BuiltModel:
    """Everything `solve.py`/`extract.py` need: the model plus every variable/row handle, keyed the
    same way `ExtractedPlan` reports them."""

    highs: highspy.Highs
    inputs: ModelInputs
    x_vars: dict[str, highspy.highs_var]
    q_vars: dict[str, highspy.highs_var]
    u_vars: dict[str, highspy.highs_var]
    n_vars: dict[str, highspy.highs_var]
    ybar_vars: dict[tuple[str, str, int], highspy.highs_var]
    h_vars: dict[tuple[str, int, str], highspy.highs_var]
    capacity_rows: dict[tuple[str, int, str], int]
    """Row index of each shared-capacity row (C1/C13), for the price-of-firmness duals."""
    lock_rows: dict[tuple[str, int], highspy.highs_cons]
    integer_vars: list[highspy.highs_var] = field(default_factory=list)
    soc_vars: dict[tuple[str, int, str], highspy.highs_var] = field(default_factory=dict)
    charge_vars: dict[tuple[str, int, str], highspy.highs_var] = field(default_factory=dict)
    soc_balance_rows: dict[tuple[str, int, str], int] = field(default_factory=dict)
    """Row index of each C1 SoC-balance row, for the stored-energy value (09 D7)."""
    solar_charge_vars: dict[tuple[str, int, str], highspy.highs_var] = field(default_factory=dict)
    terminal_soc_rows: dict[tuple[str, str], highspy.highs_cons] = field(default_factory=dict)
    costs: dict[int, float] = field(default_factory=dict)
    """Stage-F objective (09 S1.5) as column index -> coefficient (maximised)."""
    stage_r_costs: dict[int, float] = field(default_factory=dict)
    """Stage-R objective (09 D3): regulated candidates' capacity value only. Empty = single stage."""


class _RowBuffer:
    """Linear rows collected in CSR form and added in ONE `addRows` call. highspy's expression API costs
    ~0.5 ms of numpy bookkeeping per row; the two large families (capacity, SoC balance: ~23k rows at
    40 banks x 96 intervals x 3 scenarios) were most of the model build time."""

    def __init__(self) -> None:
        self._lower: list[float] = []
        self._upper: list[float] = []
        self._starts: list[int] = []
        self._index: list[int] = []
        self._value: list[float] = []

    def add(self, lower: float, upper: float, terms: list[tuple[highspy.highs_var, float]]) -> int:
        """Buffer `lower <= sum(coef * var) <= upper`; returns its position (see `flush`)."""
        position = len(self._lower)
        self._lower.append(lower)
        self._upper.append(upper)
        self._starts.append(len(self._index))
        for var, coefficient in terms:
            self._index.append(var.index)
            self._value.append(coefficient)
        return position

    def flush(self, highs: highspy.Highs) -> int:
        """Add every buffered row; returns the row index of position 0 (the rest follow in order)."""
        base = int(highs.getNumRow())
        if self._lower:
            highs.addRows(
                len(self._lower),
                np.array(self._lower, dtype=np.float64),
                np.array(self._upper, dtype=np.float64),
                len(self._index),
                np.array(self._starts, dtype=np.int32),
                np.array(self._index, dtype=np.int32),
                np.array(self._value, dtype=np.float64),
            )
        return base


def _semi_continuous_bounds(min_qty_kw: float, increment_kw: float, max_kw: float) -> tuple[float, int]:
    """Returns (effective_increment, max_steps) for q = min_qty*u + increment*n, n in [0, max_steps]."""
    if increment_kw <= _EPS:
        return 0.0, 0
    span = max(max_kw - min_qty_kw, 0.0)
    max_steps = int(span / increment_kw + _EPS)
    return increment_kw, max_steps


def build_mode_o_model(inputs: ModelInputs) -> BuiltModel:
    """Build the Mode O HiGHS model for one gate solve. Pure: no DB/MQTT/clock access."""
    highs = highspy.Highs()  # type: ignore[no-untyped-call]
    highs.silent()
    highs.setMaximize()  # type: ignore[no-untyped-call]

    bank_by_id = {b.bank_id: b for b in inputs.banks}
    dt_h = inputs.interval_hours

    x_vars: dict[str, highspy.highs_var] = {}
    q_vars: dict[str, highspy.highs_var] = {}
    u_vars: dict[str, highspy.highs_var] = {}
    n_vars: dict[str, highspy.highs_var] = {}
    ybar_vars: dict[tuple[str, str, int], highspy.highs_var] = {}
    integer_vars: list[highspy.highs_var] = []

    # --- candidate selection variables (S3.6 variable-kind mapping; C16: no scenario index) --------
    for c in inputs.candidates:
        if c.variable_kind == "BINARY":
            x_vars[c.opportunity_id] = highs.addBinary()
            integer_vars.append(x_vars[c.opportunity_id])
        elif c.variable_kind == "CONTINUOUS":
            q_vars[c.opportunity_id] = highs.addVariable(lb=0.0, ub=c.requested_kw)
        else:  # SEMI_CONTINUOUS
            u = highs.addBinary()
            integer_vars.append(u)
            u_vars[c.opportunity_id] = u
            increment, max_steps = _semi_continuous_bounds(c.min_qty_kw, c.increment_kw, c.requested_kw)
            q_vars[c.opportunity_id] = highs.addVariable(lb=0.0, ub=c.requested_kw)
            if max_steps > 0:
                n = highs.addIntegral(lb=0, ub=max_steps)
                integer_vars.append(n)
                n_vars[c.opportunity_id] = n
                highs.addConstr(q_vars[c.opportunity_id] == c.min_qty_kw * u + increment * n)
            else:
                highs.addConstr(q_vars[c.opportunity_id] == c.min_qty_kw * u)
            highs.addConstr(q_vars[c.opportunity_id] <= c.requested_kw * u)

    # --- ybar[o,b,t]: bank/interval allocation, committed and candidate obligations alike -----------
    def _bank_cap_at(bank_id: str, t: int) -> float:
        return _clamped(bank_by_id[bank_id].max_discharge_kw.get(t, 0.0))

    mobile_ids = inputs.mobile_service_ids()

    def _may_serve(obligation_id: str, bank_id: str) -> bool:
        # D-31 (`ModelInputs.may_serve`, inlined over the id set for speed at fleet scale).
        return bank_id in bank_by_id and (obligation_id not in mobile_ids or bank_by_id[bank_id].is_mobile)

    for co in inputs.committed:
        for t, kw in co.committed_kw_by_interval.items():
            eligible = [
                b
                for b in co.eligible_bank_ids
                if _may_serve(co.obligation_id, b) and t in bank_by_id[b].max_discharge_kw
            ]
            if not eligible:
                continue
            for b in eligible:
                ybar_vars[co.obligation_id, b, t] = highs.addVariable(lb=0.0, ub=_bank_cap_at(b, t))
            highs.addConstr(
                highs.qsum(ybar_vars[co.obligation_id, b, t] for b in eligible) == kw  # C24 equality
            )

    for c in inputs.candidates:
        eligible_by_t: dict[int, list[str]] = {}
        for t in c.window_intervals:
            eligible = [
                b
                for b in c.eligible_bank_ids
                if _may_serve(c.opportunity_id, b) and t in bank_by_id[b].max_discharge_kw
            ]
            eligible_by_t[t] = eligible
            for b in eligible:
                ybar_vars[c.opportunity_id, b, t] = highs.addVariable(lb=0.0, ub=_bank_cap_at(b, t))
        for t, eligible in eligible_by_t.items():
            lhs = highs.qsum(ybar_vars[c.opportunity_id, b, t] for b in eligible)
            if c.variable_kind == "BINARY":
                highs.addConstr(lhs == c.requested_kw * x_vars[c.opportunity_id])
            else:
                highs.addConstr(lhs == q_vars[c.opportunity_id])

    # --- h[b,t,w] free-headroom spot schedule + shared bank-capacity rows (K2 one-buyer, C1/C13) ----
    h_vars: dict[tuple[str, int, str], highspy.highs_var] = {}
    rows = _RowBuffer()
    capacity_positions: dict[tuple[str, int, str], int] = {}
    consumers_by_bt: dict[tuple[str, int], list[highspy.highs_var]] = {}
    for (_obligation_id, bank_id, t), var in ybar_vars.items():
        consumers_by_bt.setdefault((bank_id, t), []).append(var)

    for bank in inputs.banks:
        for t, raw_cap_kw in bank.max_discharge_kw.items():
            cap_kw = _clamped(raw_cap_kw)
            # K15(b): no FREE (ERCOT) headroom from a regulated-territory bank without wholesale access.
            headroom_cap_kw = cap_kw if bank.free_market_access else 0.0
            consumers = consumers_by_bt.get((bank.bank_id, t), [])
            for scenario in inputs.scenarios:
                h = highs.addVariable(lb=0.0, ub=headroom_cap_kw)
                h_vars[bank.bank_id, t, scenario.scenario] = h
                capacity_positions[bank.bank_id, t, scenario.scenario] = rows.add(
                    -highspy.kHighsInf, cap_kw, [(v, 1.0) for v in (*consumers, h)]
                )

    # --- SoC dynamics (02a S3.2/S3.3 C1 energy balance, C2 SOC bounds, C15 terminal energy) --------
    # Per-bank, per-scenario energy state, added only for banks carrying an energy envelope
    # (`BankSnapshot.models_soc`) -- see that property's docstring for why this is backward compatible
    # with every caller that only ever supplied a power envelope. The affine step below is exactly
    # `opengrid.core.physics.soc_step`'s formula (its clamp to [0, e_kwh] becomes these LP variables'
    # own bounds instead of a runtime min/max, since HiGHS constraints must stay linear); the
    # coefficients (`eta_c`, `eta_d`, `self_discharge_kwh_per_h`) are `BankSnapshot`'s copies of
    # `core.physics`'s constants, never re-derived. No linear-coefficient helper exists in `core` for
    # this step today -- see the module's final-report note on adding one so `allocator`/`guardian`
    # can share it too instead of each inlining the same affine form.
    # ERCOT_AS capacity holds (NPRR1282): their ybar locks bank kW (the capacity rows above) but does NOT
    # drain SoC; instead the bank keeps kW x hold_h / eta_d above its reserve floor while held.
    hold_h_by_id = inputs.energy_hold_hours()
    committed_ids = {co.obligation_id for co in inputs.committed}
    drain_by_bt: dict[tuple[str, int], list[highspy.highs_var]] = {}
    hold_by_bt: dict[tuple[str, int], list[tuple[highspy.highs_var, float, bool]]] = {}
    for (obligation_id, bank_id, t), var in ybar_vars.items():
        hold_h = hold_h_by_id.get(obligation_id, 0.0)
        if hold_h > 0:
            hold_by_bt.setdefault((bank_id, t), []).append((var, hold_h, obligation_id in committed_ids))
        else:
            drain_by_bt.setdefault((bank_id, t), []).append(var)
    hold_slack_vars: list[tuple[str, highspy.highs_var]] = []
    # 09 C7(b)' / D13 no wash trade: REGULATED deliveries on each (bank, t), which bar charging there.
    regulated_ids = inputs.regulated_obligation_ids()
    regulated_by_bt: dict[tuple[str, int], list[highspy.highs_var]] = {}
    for (obligation_id, bank_id, t), var in ybar_vars.items():
        if obligation_id in regulated_ids:
            regulated_by_bt.setdefault((bank_id, t), []).append(var)

    soc_vars: dict[tuple[str, int, str], highspy.highs_var] = {}
    charge_vars: dict[tuple[str, int, str], highspy.highs_var] = {}
    solar_charge_vars: dict[tuple[str, int, str], highspy.highs_var] = {}
    soc_balance_positions: dict[tuple[str, int, str], int] = {}
    terminal_soc_rows: dict[tuple[str, str], highspy.highs_cons] = {}
    terminal_shortfall_vars: dict[tuple[str, str], highspy.highs_var] = {}

    for bank in inputs.banks:
        if not bank.models_soc:
            continue
        intervals = sorted(bank.max_discharge_kw)
        if not intervals:
            continue
        for scenario in inputs.scenarios:
            first_t = intervals[0]
            soc_vars[bank.bank_id, first_t, scenario.scenario] = highs.addVariable(
                lb=bank.reserve_kwh, ub=bank.capacity_kwh
            )
            highs.addConstr(soc_vars[bank.bank_id, first_t, scenario.scenario] == bank.initial_soc_kwh)

            for t in intervals:
                # D-31: a mobile unit charges (grid or solar) only while at its home station.
                charge_cap_kw = _clamped(bank.max_charge_kw.get(t, 0.0)) if bank.charging_allowed(t) else 0.0
                charge = highs.addVariable(lb=0.0, ub=charge_cap_kw if bank.grid_charge_allowed(t) else 0.0)
                charge_vars[bank.bank_id, t, scenario.scenario] = charge
                charging: list[tuple[highspy.highs_var, float]] = [(charge, 1.0)]
                solar_kw = min(_clamped(bank.solar_charge_kw.get(t, 0.0)), charge_cap_kw)
                if solar_kw > 0.0:
                    # 09 C27: g = g_sol + g_grid, both within the one charge envelope.
                    solar = highs.addVariable(lb=0.0, ub=solar_kw)
                    solar_charge_vars[bank.bank_id, t, scenario.scenario] = solar
                    charging.append((solar, 1.0))
                    rows.add(-highspy.kHighsInf, charge_cap_kw, charging)
                headroom = h_vars[bank.bank_id, t, scenario.scenario]
                cap_kw = _bank_cap_at(bank.bank_id, t)
                headroom_cap_kw = cap_kw if bank.free_market_access else 0.0
                if (
                    charge_cap_kw > 0.0
                    and headroom_cap_kw > 0.0
                    and bank.grid_charge_allowed(t)
                    and _wash_pays(bank, scenario.price_at(bank.bank_id, t), t)
                ):
                    # No arbitrage wash (owner rule): never buy from the grid and sell headroom in the same
                    # interval and market. Elsewhere netting strictly beats it (losses, wear, M1), so the
                    # either/or binary is added only where a wash could pay or tie (deeply negative prices).
                    buying = highs.addBinary()
                    integer_vars.append(buying)
                    rows.add(-highspy.kHighsInf, 0.0, [(charge, 1.0), (buying, -charge_cap_kw)])
                    rows.add(
                        -highspy.kHighsInf, headroom_cap_kw, [(headroom, 1.0), (buying, headroom_cap_kw)]
                    )
                if charge_cap_kw > 0.0 and cap_kw > 0.0:
                    for delivery in regulated_by_bt.get((bank.bank_id, t), []):
                        # C7(b)' (D13): no charging while this bank delivers a regulated obligation; a
                        # partial delivery leaves the proportional rest of the charge envelope.
                        rows.add(
                            -highspy.kHighsInf,
                            charge_cap_kw,
                            [*charging, (delivery, charge_cap_kw / cap_kw)],
                        )
                discharging = [*drain_by_bt.get((bank.bank_id, t), []), headroom]
                holds = hold_by_bt.get((bank.bank_id, t), [])
                if holds:
                    # Energy hold: SoC at the start of t covers every held award's full deployment. A
                    # COMMITTED hold the bank cannot cover is a priced slack (never infeasible: the award
                    # is already locked, K13); a CANDIDATE hold is hard, so it is only selected where the
                    # energy exists.
                    committed_hold_cap_kwh = sum(
                        hold_h / bank.eta_d * _bank_cap_at(bank.bank_id, t)
                        for _var, hold_h, is_committed in holds
                        if is_committed
                    )
                    # Matches the guardian's G-01-ENERGY floor (1% of capacity above reserve), else the
                    # last leases of a full deployment are vetoed shortly before its end. Only with a
                    # committed hold present (a candidate-only row at q=0 must not bind).
                    margin_kwh = (
                        AS_HOLD_FLOOR_FRACTION * bank.capacity_kwh if committed_hold_cap_kwh > 0 else 0.0
                    )
                    slack = highs.addVariable(lb=0.0, ub=committed_hold_cap_kwh + margin_kwh)
                    hold_slack_vars.append((scenario.scenario, slack))
                    required = highs.qsum([(hold_h / bank.eta_d) * var for var, hold_h, _c in holds])
                    highs.addConstr(
                        soc_vars[bank.bank_id, t, scenario.scenario] + slack
                        >= bank.reserve_kwh + margin_kwh + required
                    )
                next_e = highs.addVariable(lb=bank.reserve_kwh, ub=bank.capacity_kwh)
                soc_vars[bank.bank_id, t + 1, scenario.scenario] = next_e
                # next_e == e + eta_c*g*dt - (dt/eta_d)*discharge - self_discharge*dt, as one buffered row.
                rhs = -bank.self_discharge_kwh_per_h * dt_h
                soc_balance_positions[bank.bank_id, t, scenario.scenario] = rows.add(
                    rhs,
                    rhs,
                    [
                        (next_e, 1.0),
                        (soc_vars[bank.bank_id, t, scenario.scenario], -1.0),
                        *((var, -bank.eta_c * dt_h) for var, _unit in charging),
                        *((var, dt_h / bank.eta_d) for var in discharging),
                    ],
                )

            # C15 as a penalized target, not a hard floor: `terminal SoC >= initial` was infeasible
            # whenever the bank cannot recharge within the horizon (no charge headroom, or just the
            # unavoidable self-discharge), which forced every live gate to RULE_FALLBACK (2026-09-26).
            # The shortfall below the floor costs TERMINAL_SHORTFALL_PENALTY_USD_PER_KWH, above any
            # MVP-S energy value, so the LP only dips below it when a hard constraint requires it.
            terminal_t = intervals[-1] + 1
            shortfall = highs.addVariable(lb=0.0)
            terminal_shortfall_vars[bank.bank_id, scenario.scenario] = shortfall
            terminal_soc_rows[bank.bank_id, scenario.scenario] = highs.addConstr(
                soc_vars[bank.bank_id, terminal_t, scenario.scenario] + shortfall
                >= bank.initial_soc_kwh - inputs.terminal_soc_slack_kwh
            )

    base_row = rows.flush(highs)
    capacity_rows = {key: base_row + position for key, position in capacity_positions.items()}
    soc_balance_rows = {key: base_row + position for key, position in soc_balance_positions.items()}

    # --- objective (02a S3.4 / 09 S1.5 stage F) -----------------------------------------------------
    # Set as column costs, never by summing expressions: `expr + term` copies the whole expression, so
    # the old accumulation was quadratic (44 of 52 s of a 40-bank/96-interval build, KPI 30 s).
    costs: dict[int, float] = {}
    stage_r_costs: dict[int, float] = {}

    def _add_cost(target: dict[int, float], var: highspy.highs_var, cost: float) -> None:
        target[var.index] = target.get(var.index, 0.0) + cost

    value_per_kwh = {c.opportunity_id: c.value_per_mwh / 1000.0 for c in inputs.candidates}
    regulated_candidate_ids = {c.opportunity_id for c in inputs.candidates if c.is_regulated}
    deployment_share = inputs.expected_deployment_shares()
    for (obligation_id, bank_id, _t), var in ybar_vars.items():
        value = value_per_kwh.get(obligation_id, 0.0)  # committed: sunk value, locked by C24 anyway
        # 09 D8 (Frank #7): wear on every kWh a delivery DISCHARGES, whatever the service. A capacity hold
        # (AS award, regulated need-basis reserve) discharges only its expected deployment (psi * r).
        discharged_share = deployment_share.get(obligation_id, 0.0) if obligation_id in hold_h_by_id else 1.0
        wear = discharged_share * wear_usd_per_kwh(bank_by_id[bank_id])
        _add_cost(costs, var, (value - wear) * dt_h)
        if obligation_id in regulated_candidate_ids:
            _add_cost(stage_r_costs, var, value * dt_h)  # 09 D3 stage R: regulated capacity value only
    for scenario in inputs.scenarios:
        for bank in inputs.banks:
            wear = wear_usd_per_kwh(bank)
            for t in bank.max_discharge_kw:
                price = scenario.price_at(bank.bank_id, t)
                weight = scenario.probability * dt_h
                _add_cost(costs, h_vars[bank.bank_id, t, scenario.scenario], weight * (price / 1000.0 - wear))
                charge = charge_vars.get((bank.bank_id, t, scenario.scenario))
                if charge is not None:
                    # 09 C27/D5: a grid-drawn kWh costs the zone price + M1 in the competitive area, the
                    # utility's grid charging rate in a regulated territory (no M1). Exports never
                    # recover M1 (no credit on `h`).
                    _add_cost(costs, charge, -weight * bank.charge_cost_usd_per_kwh(price, t))
                solar = solar_charge_vars.get((bank.bank_id, t, scenario.scenario))
                if solar is not None:
                    # Solar never pays M1: the utility's solar price, or the forgone PV export credit.
                    solar_cost = bank.solar_cost_usd_per_kwh
                    _add_cost(costs, solar, -weight * (price / 1000.0 if solar_cost is None else solar_cost))

    probability_by_scenario: dict[str, float] = {s.scenario: s.probability for s in inputs.scenarios}
    for scenario_name, slack in hold_slack_vars:
        _add_cost(
            costs, slack, -probability_by_scenario[scenario_name] * TERMINAL_SHORTFALL_PENALTY_USD_PER_KWH
        )
    for (_bank_id, scenario_name), shortfall in terminal_shortfall_vars.items():
        _add_cost(
            costs, shortfall, -probability_by_scenario[scenario_name] * TERMINAL_SHORTFALL_PENALTY_USD_PER_KWH
        )
    set_column_costs(highs, costs)

    return BuiltModel(
        highs=highs,
        inputs=inputs,
        x_vars=x_vars,
        q_vars=q_vars,
        u_vars=u_vars,
        n_vars=n_vars,
        ybar_vars=ybar_vars,
        h_vars=h_vars,
        capacity_rows=capacity_rows,
        lock_rows={},
        integer_vars=integer_vars,
        soc_vars=soc_vars,
        charge_vars=charge_vars,
        soc_balance_rows=soc_balance_rows,
        solar_charge_vars=solar_charge_vars,
        terminal_soc_rows=terminal_soc_rows,
        costs=costs,
        stage_r_costs=stage_r_costs,
    )


def set_column_costs(highs: highspy.Highs, costs: dict[int, float]) -> None:
    """Replace the objective with `costs` (column index -> coefficient); columns not named cost 0."""
    n_cols = highs.getNumCol()
    dense = np.zeros(n_cols, dtype=np.float64)
    for index, cost in costs.items():
        dense[index] = cost
    highs.changeColsCost(n_cols, np.arange(n_cols, dtype=np.int32), dense)
