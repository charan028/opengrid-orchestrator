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

from opengrid.selector.types import ModelInputs

_EPS = 1e-9
#: C15 terminal-energy shortfall cost ($/kWh below the next-day floor, i.e. $1,000/MWh): above every
#: MVP-S energy/capacity value, so energy is only drawn below the floor when a hard constraint needs it.
TERMINAL_SHORTFALL_PENALTY_USD_PER_KWH = 1.0
_MIN_MEANINGFUL_KW = 1e-6  # below this, treat capacity as exactly 0 -- avoids HiGHS "tiny coefficient"
# numerical errors on pathologically small (but nonzero) capacity readings.


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
    capacity_rows: dict[tuple[str, int, str], highspy.highs_cons]
    lock_rows: dict[tuple[str, int], highspy.highs_cons]
    integer_vars: list[highspy.highs_var] = field(default_factory=list)
    soc_vars: dict[tuple[str, int, str], highspy.highs_var] = field(default_factory=dict)
    charge_vars: dict[tuple[str, int, str], highspy.highs_var] = field(default_factory=dict)
    soc_balance_rows: dict[tuple[str, int, str], highspy.highs_cons] = field(default_factory=dict)
    terminal_soc_rows: dict[tuple[str, str], highspy.highs_cons] = field(default_factory=dict)


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

    for co in inputs.committed:
        for t, kw in co.committed_kw_by_interval.items():
            eligible = [
                b for b in co.eligible_bank_ids if b in bank_by_id and t in bank_by_id[b].max_discharge_kw
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
                b for b in c.eligible_bank_ids if b in bank_by_id and t in bank_by_id[b].max_discharge_kw
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
    capacity_rows: dict[tuple[str, int, str], highspy.highs_cons] = {}
    consumers_by_bt: dict[tuple[str, int], list[highspy.highs_var]] = {}
    for (_obligation_id, bank_id, t), var in ybar_vars.items():
        consumers_by_bt.setdefault((bank_id, t), []).append(var)

    for bank in inputs.banks:
        for t, raw_cap_kw in bank.max_discharge_kw.items():
            cap_kw = _clamped(raw_cap_kw)
            consumers = consumers_by_bt.get((bank.bank_id, t), [])
            for scenario in inputs.scenarios:
                h = highs.addVariable(lb=0.0, ub=cap_kw)
                h_vars[bank.bank_id, t, scenario.scenario] = h
                lhs = highs.qsum([*consumers, h])
                capacity_rows[bank.bank_id, t, scenario.scenario] = highs.addConstr(lhs <= cap_kw)

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

    soc_vars: dict[tuple[str, int, str], highspy.highs_var] = {}
    charge_vars: dict[tuple[str, int, str], highspy.highs_var] = {}
    soc_balance_rows: dict[tuple[str, int, str], highspy.highs_cons] = {}
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
                charge = highs.addVariable(lb=0.0, ub=_clamped(bank.max_charge_kw.get(t, 0.0)))
                charge_vars[bank.bank_id, t, scenario.scenario] = charge
                discharge_total = highs.qsum(
                    [
                        *drain_by_bt.get((bank.bank_id, t), []),
                        h_vars[bank.bank_id, t, scenario.scenario],
                    ]
                )
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
                    slack = highs.addVariable(lb=0.0, ub=committed_hold_cap_kwh)
                    hold_slack_vars.append((scenario.scenario, slack))
                    required = highs.qsum([(hold_h / bank.eta_d) * var for var, hold_h, _c in holds])
                    highs.addConstr(
                        soc_vars[bank.bank_id, t, scenario.scenario] + slack >= bank.reserve_kwh + required
                    )
                next_e = highs.addVariable(lb=bank.reserve_kwh, ub=bank.capacity_kwh)
                soc_vars[bank.bank_id, t + 1, scenario.scenario] = next_e
                soc_balance_rows[bank.bank_id, t, scenario.scenario] = highs.addConstr(
                    next_e
                    == soc_vars[bank.bank_id, t, scenario.scenario]
                    + bank.eta_c * charge * dt_h
                    - (dt_h / bank.eta_d) * discharge_total
                    - bank.self_discharge_kwh_per_h * dt_h
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

    # --- objective (02a S3.4, MVP-S subset) ---------------------------------------------------------
    vars_by_obligation: dict[str, list[highspy.highs_var]] = {}
    for (obligation_id, _bank_id, _t), var in ybar_vars.items():
        vars_by_obligation.setdefault(obligation_id, []).append(var)

    obj_terms = []
    for c in inputs.candidates:
        obligation_vars = vars_by_obligation.get(c.opportunity_id, [])
        if not obligation_vars:
            continue
        total_kw = highs.qsum(obligation_vars)
        # `degradation_cost_per_kwh` (02a S3.4's c_deg) prices the wear of actually *cycling* the
        # battery -- it belongs on `MARKET` candidates (ERCOT_ENERGY spot arbitrage: charge low,
        # discharge high, real round-trip throughput every interval). `FIRM` (HOME/DIST_DEFERRAL/
        # PARTNER_CAPACITY) and `AS` (ERCOT_AS) candidates are *capacity holds* -- their value
        # (`value_per_mwh`: a capacity payment or MCPC, not an energy-arbitrage spread) is priced per
        # 02a S1's C3 as its own additive floor, separate from the cycling economics (spec line "C3 |
        # The ONE additive floor (AS hold + robust firm energy)"). Charging them the full per-kWh
        # cycling degradation anyway made every low-$/MWh capacity-hold candidate's objective
        # coefficient strictly negative (e.g. a $5.37/MWh AS MCPC minus a $30/MWh-equivalent
        # degradation cost) regardless of available headroom -- `x_o=0`/`q_o=0` was the solver's
        # correct answer given that flawed input, identical in kind to the DIST_DEFERRAL
        # `value_per_mwh=None` bug this same objective already had (`qa/merge-notes.md` S15).
        degradation_usd_per_kwh = c.degradation_cost_per_kwh if c.category == "MARKET" else 0.0
        obj_terms.append((c.value_per_mwh / 1000.0 - degradation_usd_per_kwh) * dt_h * total_kw)
    for co in inputs.committed:
        obligation_vars = vars_by_obligation.get(co.obligation_id, [])
        if not obligation_vars:
            continue
        total_kw = highs.qsum(obligation_vars)
        obj_terms.append(-co.degradation_cost_per_kwh * dt_h * total_kw)
    for scenario in inputs.scenarios:
        for bank in inputs.banks:
            for t in bank.max_discharge_kw:
                price = scenario.price_usd_per_mwh.get(t, 0.0)
                h = h_vars[bank.bank_id, t, scenario.scenario]
                obj_terms.append(scenario.probability * dt_h * (price / 1000.0) * h)
                charge = charge_vars.get((bank.bank_id, t, scenario.scenario))
                if charge is not None:
                    # Charging draws from the grid at the same price signal (02a S3.4's -(v^E+w_b)*g;
                    # w_b, a per-bank wheeling tariff, is not yet a modeled parameter anywhere in this
                    # codebase -- see the module's final-report note).
                    obj_terms.append(-scenario.probability * dt_h * (price / 1000.0) * charge)

    probability_by_scenario: dict[str, float] = {s.scenario: s.probability for s in inputs.scenarios}
    for scenario_name, slack in hold_slack_vars:
        obj_terms.append(
            -probability_by_scenario[scenario_name] * TERMINAL_SHORTFALL_PENALTY_USD_PER_KWH * slack
        )
    for (_bank_id, scenario_name), shortfall in terminal_shortfall_vars.items():
        obj_terms.append(
            -probability_by_scenario[scenario_name] * TERMINAL_SHORTFALL_PENALTY_USD_PER_KWH * shortfall
        )

    if obj_terms:
        objective = obj_terms[0]
        for term in obj_terms[1:]:
            objective = objective + term
        highs.setObjective(objective)

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
        terminal_soc_rows=terminal_soc_rows,
    )
