"""Mode O model builder (02a S3.2-S3.7 subset). Pure function: `ModelInputs` -> a highspy `Highs` model.

No solving, no I/O -- `solve.py` runs it, `extract.py` reads the solution back into `ExtractedPlan`.

**Deliverable-boundary simplification (documented, not hacked around).** Full physics (SoC dynamics,
charge-side constraints C7-C9, charge/discharge exclusivity C14, terminal-energy C15, cycle budget C18)
live on the other side of the `fleet.capability`/`forecast.scenarios` interface boundary (02b S4/S3):
`fleet` already reduces hub/bank SoC state to a discharge-capability envelope per bank/interval before
the selector ever sees it (02b S12 "fleet never simulates -- it only stores and aggregates"), and the
review's own architecture puts SoC dynamics in `core.physics`/`fleet`, not `selector`. Re-deriving them
here would violate BUILD.md S1's no-duplicated-functions rule and duplicate the `fleet`/`allocator`
owners' work. This builder therefore implements the constraint families that are genuinely the
selector's to decide: C1 (aggregate bank capacity balance, reduced to the discharge envelope), C6/C13
(one obligation-set per bank/interval, i.e. K2 one-buyer, materialized as a shared-capacity row), C12
(firm delivery + locality via eligible-bank sets), C16 (non-anticipativity, structural: first-stage
variables carry no scenario index), and C24 (the commitment lock, an equality on the frozen total).
C3/C11 (AS-vs-firm exclusivity beyond shared-capacity competition), C17 (reserve-deficit recovery) and
the piecewise penalty term are deferred; see the module's final-report note.

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
        obj_terms.append((c.value_per_mwh / 1000.0 - c.degradation_cost_per_kwh) * dt_h * total_kw)
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
    )
