"""Gate orchestration (02a S3.1/S3.8): assembles `ModelInputs`, solves Mode O, validates, falls back to
F2, persists the `plan` row, and transitions opportunities through `contracts`/`ledger`.

Each I/O step is a small, separately named async function so tests can monkeypatch exactly the seam
they need (BUILD.md "use fakes for siblings") without touching the pure `model`/`solve`/`extract`/
`validate`/`rule_fallback` core, which is tested directly with hand-built `ModelInputs`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from opengrid import forecast, ledger
from opengrid.core.models.engine import Plan
from opengrid.core.physics import DEFAULT_ETA_C, DEFAULT_ETA_D
from opengrid.fleet import capability as fleet_capability
from opengrid.fleet import hub_capabilities as fleet_hub_capabilities
from opengrid.selector import db
from opengrid.selector.extract import extract_plan
from opengrid.selector.model import build_mode_o_model
from opengrid.selector.rule_fallback import rule_fallback_f2
from opengrid.selector.solve import highs_solve
from opengrid.selector.types import (
    BankSnapshot,
    CandidateOpportunity,
    CommittedObligation,
    ExtractedPlan,
    GateKind,
    ModelInputs,
    ScenarioPrice,
    solver_settings_for,
)
from opengrid.selector.validate import validate_plan

INTERVAL_MINUTES = 15.0
SCHEDULED_HORIZON_INTERVALS = 96  # 24h / 15min, 02a S3.2

# 02a S3.7's "firm first, then AS, then market" F2 priority bucket, keyed by `contract.service_type`
# (`CandidateOpportunity.category`'s docstring). `ERCOT_ENERGY` (spot-like) falls back to `MARKET`.
_CATEGORY_BY_SERVICE_TYPE: dict[str, Literal["FIRM", "AS", "MARKET"]] = {
    "HOME": "FIRM",
    "DIST_DEFERRAL": "FIRM",
    "PARTNER_CAPACITY": "FIRM",
    "ERCOT_AS": "AS",
    "ERCOT_ENERGY": "MARKET",
}

# Simple warm-start memory: previous gate's selection, shifted one interval by the caller if needed
# (02a S3.7 "previous plan shifted one interval"). Kept in-process only -- a restart just solves cold.
_last_hint_x: dict[str, float] = {}
_last_hint_q: dict[str, float] = {}


async def compute_horizon(gate_kind: GateKind, now: datetime) -> tuple[datetime, datetime]:
    """24h horizon for `SCHEDULED_15MIN`/`ADMISSION`; `RENOMINATION` narrows to the obligation's own
    window in `run_gate` once its `contract_scope` is resolved (02a S3.1's scope-of-re-optimization
    column), so this returns the same 24h default for all three and the caller trims it."""
    return now, now + timedelta(hours=24)


def _bank_energy_envelope(bank_id: str) -> tuple[float, float, float, float, float]:
    """Live per-bank energy envelope from the fleet twin (user requirement: "energy above reserve
    must be checked continuously" -- the selector must use the LIVE initial SoC per bank from the
    twin, not a guessed/zero one). Aggregates `fleet.hub_capabilities(bank_id)` (already exposing
    `soc_kwh`/`reserve_kwh`/`e_kwh`/`eta_d` per hub for exactly this purpose, per that function's own
    docstring) over the hubs currently reporting live state (`soc_kwh is not None`, i.e. "online" this
    instant) -- never over every configured hub regardless of health, which would silently invent an
    unobserved SoC for an offline/stale hub.

    Deliberately hardware-agnostic (no per-service or per-hub-model special-casing): whatever mix of
    hub sizes a bank actually has (e.g. 39.2 kWh/11 kW single-unit vs 78.4 kWh/20 kW 2-unit homes) is
    summed as reported by the twin, never assumed.

    Returns `(capacity_kwh, reserve_kwh, initial_soc_kwh, eta_c, eta_d)`. When no hub on this bank is
    currently online, returns `(0.0, 0.0, 0.0, DEFAULT_ETA_C, DEFAULT_ETA_D)` -- `BankSnapshot.capacity_kwh
    <= 0` is the documented sentinel for "no energy envelope this cycle", which cleanly skips SoC
    modeling for the bank (K7 degrade, don't trip) instead of pinning `soc == initial_soc` against
    `[reserve_kwh, capacity_kwh]` bounds computed from a *different, larger* hub set than the live
    reading -- which would make the model spuriously `INFEASIBLE_F1` under a partial/total fleet outage
    (confirmed live 2026-09-25/26: every hub across all 40 banks went "offline" during an `og-engine`
    restart loop)."""
    hubs = fleet_hub_capabilities(bank_id)
    online = [h for h in hubs if h.soc_kwh is not None and h.reserve_kwh is not None and h.e_kwh is not None]
    if not online:
        return 0.0, 0.0, 0.0, DEFAULT_ETA_C, DEFAULT_ETA_D
    capacity_kwh = sum(h.e_kwh for h in online if h.e_kwh is not None)
    reserve_kwh = sum(h.reserve_kwh for h in online if h.reserve_kwh is not None)
    initial_soc_kwh = sum(h.soc_kwh for h in online if h.soc_kwh is not None)
    eta_d = sum(h.eta_d for h in online) / len(online)
    return capacity_kwh, reserve_kwh, initial_soc_kwh, DEFAULT_ETA_C, eta_d


async def load_banks(
    horizon_start: datetime, horizon_end: datetime, bank_ids: tuple[str, ...]
) -> tuple[BankSnapshot, ...]:
    """Bank discharge-capability snapshot via `fleet.capability` (02b S4), one call per bank/interval,
    plus the bank's live energy envelope (`_bank_energy_envelope`, once per bank -- current SoC/
    capacity/reserve are a live-now reading, not something that varies per future horizon interval)."""
    n_intervals = int((horizon_end - horizon_start).total_seconds() // (INTERVAL_MINUTES * 60))
    snapshots = []
    for bank_id in bank_ids:
        by_interval: dict[int, float] = {}
        for t in range(n_intervals):
            interval_start = horizon_start + timedelta(minutes=INTERVAL_MINUTES * t)
            cap = await fleet_capability(bank_id, interval_start)
            by_interval[t] = cap.max_discharge_kw
        capacity_kwh, reserve_kwh, initial_soc_kwh, eta_c, eta_d = _bank_energy_envelope(bank_id)
        snapshots.append(
            BankSnapshot(
                bank_id=bank_id,
                max_discharge_kw=by_interval,
                capacity_kwh=capacity_kwh,
                reserve_kwh=reserve_kwh,
                initial_soc_kwh=initial_soc_kwh,
                eta_c=eta_c,
                eta_d=eta_d,
            )
        )
    return tuple(snapshots)


async def load_scenarios(horizon_start: datetime, horizon_end: datetime) -> tuple[ScenarioPrice, ...]:
    """P10/P50/P90 price scenarios via `forecast.scenarios` (02b S3)."""
    points = await forecast.scenarios(horizon_start, horizon_end)
    by_scenario: dict[str, dict[str, list[tuple[int, float]]]] = {}
    probability_by_scenario: dict[str, float] = {}
    for point in points:
        t = int((point.interval_start - horizon_start).total_seconds() // (INTERVAL_MINUTES * 60))
        by_scenario.setdefault(point.scenario, {}).setdefault("prices", []).append((t, point.value))
        probability_by_scenario[point.scenario] = point.probability
    return tuple(
        ScenarioPrice(
            scenario=name,  # type: ignore[arg-type]
            probability=probability_by_scenario[name],
            price_usd_per_mwh=dict(data["prices"]),
        )
        for name, data in by_scenario.items()
    )


async def load_committed(
    horizon_start: datetime, horizon_end: datetime, bank_ids: tuple[str, ...]
) -> tuple[CommittedObligation, ...]:
    """Committed/delivering obligations overlapping the horizon, frozen per `db.load_frozen_commitments`
    (02a S2.2). All configured banks are eligible for redistribution (bank *substitution*, not a
    reduction -- see `types.CommittedObligation`); a future refinement can narrow this per obligation
    once `contracts`/`ledger` expose an eligibility query."""
    frozen = await db.load_frozen_commitments(horizon_start.isoformat(), horizon_end.isoformat())
    n_intervals = int((horizon_end - horizon_start).total_seconds() // (INTERVAL_MINUTES * 60))
    interval_index_by_iso = {
        (horizon_start + timedelta(minutes=INTERVAL_MINUTES * t)).isoformat(): t for t in range(n_intervals)
    }
    result = []
    for obligation_id, by_interval_iso in frozen.items():
        by_index = {
            interval_index_by_iso[iso]: kw
            for iso, kw in by_interval_iso.items()
            if iso in interval_index_by_iso
        }
        if by_index:
            result.append(
                CommittedObligation(
                    obligation_id=str(obligation_id),
                    eligible_bank_ids=bank_ids,
                    committed_kw_by_interval=by_index,
                )
            )
    return tuple(result)


async def load_candidates(
    horizon_start: datetime, horizon_end: datetime, bank_ids: tuple[str, ...], contract_scope: UUID | None
) -> tuple[CandidateOpportunity, ...]:
    """`OFFERED` opportunities for the horizon (`ADMISSION`/`RENOMINATION` narrow via `contract_scope`),
    read from `og.opportunity`/`og.contract`/`og.product_rule` via `selector.db` (that module's own
    docstring already scopes selector to read those tables read-only). `contracts` exposes no public
    opportunity-listing query beyond `admit`/`product_rules_for` (INTERFACES.md's fixed four) -- see the
    module's final-report note asking the merge agent to add one there instead, so this reads the
    tables directly rather than staying a permanent placeholder.

    Every configured bank is eligible for every candidate: no `contracts`/`ledger` query exists yet to
    narrow eligibility per opportunity either (`load_committed`'s docstring documents the identical gap
    for committed obligations)."""
    rows = await db.load_offered_opportunities_rows(
        horizon_start.isoformat(), horizon_end.isoformat(), contract_scope
    )
    n_intervals = int((horizon_end - horizon_start).total_seconds() // (INTERVAL_MINUTES * 60))
    candidates = []
    for row in rows:
        window_start = max(row["window_start"], horizon_start)
        window_end = min(row["window_end"], horizon_end)
        start_t = int((window_start - horizon_start).total_seconds() // (INTERVAL_MINUTES * 60))
        end_t = int((window_end - horizon_start).total_seconds() // (INTERVAL_MINUTES * 60))
        window_intervals = tuple(range(max(start_t, 0), min(end_t, n_intervals)))
        if not window_intervals:
            continue  # window does not actually overlap this horizon after clamping/rounding
        candidates.append(
            CandidateOpportunity(
                opportunity_id=str(row["opportunity_id"]),
                obligation_id=str(row["obligation_id"]),
                contract_id=str(row["contract_id"]),
                eligible_bank_ids=bank_ids,
                window_intervals=window_intervals,
                requested_kw=float(row["requested_kw"]),
                value_per_mwh=float(row["value_per_mwh"]) if row["value_per_mwh"] is not None else 0.0,
                variable_kind=row["variable_kind"] or "CONTINUOUS",
                min_qty_kw=float(row["min_qty_kw"] or 0.0),
                increment_kw=float(row["increment_kw"] or 0.0),
                degradation_cost_per_kwh=float(row["degradation_cost"] or 0.03),
                tier=row["tier"] or "T4",
                category=_CATEGORY_BY_SERVICE_TYPE.get(row["service_type"], "MARKET"),
            )
        )
    return tuple(candidates)


def _plan_mode_for(gate_kind: GateKind, horizon_start: datetime) -> str:
    if gate_kind == "SCHEDULED_15MIN" and horizon_start.hour == 0 and horizon_start.minute < INTERVAL_MINUTES:
        return "L-DA"
    return "L-ID"


async def persist_plan(
    plan_mode: str,
    gate_kind: GateKind,
    horizon_start: datetime,
    horizon_end: datetime,
    scenarios: tuple[ScenarioPrice, ...],
    result: ExtractedPlan,
) -> UUID:
    """Insert the `og.plan` row (02a S1.8) and return its id."""
    plan_id = uuid4()
    pool = await db.get_pool()
    scenario_set = json.dumps([{"scenario": s.scenario, "prob": s.probability} for s in scenarios])
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO og.plan (plan_id, plan_mode, gate_kind, horizon_start, horizon_end,
                                  scenario_set, solver_status, solver_gap, solver_time_ms, objective_value)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                plan_id,
                plan_mode,
                gate_kind,
                horizon_start,
                horizon_end,
                scenario_set,
                result.solver_status,
                result.solver_gap,
                result.solver_time_ms,
                Decimal(str(result.objective_value)),
            ),
        )
    return plan_id


def solve_gate(inputs: ModelInputs, gate_kind: GateKind, horizon_start: datetime) -> ExtractedPlan:
    """The solver core (02a S3.8, no I/O): build, solve, validate, fall back to F2 if needed. Exercised
    directly by unit/property tests against hand-built `ModelInputs`; `run_gate` wraps it with the
    DB/`contracts`/`ledger`/`fleet`/`forecast` I/O the fixed interface requires end to end."""
    plan_mode = _plan_mode_for(gate_kind, horizon_start)
    settings = solver_settings_for(gate_kind)
    built = build_mode_o_model(inputs)
    outcome = highs_solve(built, settings, x_hint=_last_hint_x, q_hint=_last_hint_q)
    result = extract_plan(built, outcome, plan_mode)

    if outcome.status in ("INFEASIBLE_F1", "TIME_LIMIT_GAP"):
        return rule_fallback_f2(inputs)

    ok, _violations = validate_plan(inputs, result)
    if not ok:
        return rule_fallback_f2(inputs)
    return result


async def run_gate(gate_kind: GateKind, contract_scope: UUID | None = None) -> Plan:
    """Run one selector gate (02a S3.1/S3.8): assemble inputs, solve, validate, fall back if needed,
    persist the plan, and transition every candidate opportunity through `contracts`/`ledger`.

    Never reduces a `COMMITTED`/`DELIVERING` obligation's frozen `commitment.committed_kw` (K13):
    committed obligations are injected as C24 equality parameters (`load_committed`), not re-decided.
    """
    if gate_kind == "RENOMINATION" and contract_scope is None:
        raise ValueError("RENOMINATION requires contract_scope (02a S1.7)")

    now = datetime.now(UTC)
    horizon_start, horizon_end = await compute_horizon(gate_kind, now)
    bank_ids = tuple(sorted(set(await _configured_bank_ids())))

    banks, scenarios, committed, candidates = (
        await load_banks(horizon_start, horizon_end, bank_ids),
        await load_scenarios(horizon_start, horizon_end),
        await load_committed(horizon_start, horizon_end, bank_ids),
        await load_candidates(horizon_start, horizon_end, bank_ids, contract_scope),
    )

    n_intervals = int((horizon_end - horizon_start).total_seconds() // (INTERVAL_MINUTES * 60))
    inputs = ModelInputs(
        intervals=tuple(range(n_intervals)),
        interval_minutes=INTERVAL_MINUTES,
        banks=banks,
        scenarios=scenarios,
        committed=committed,
        candidates=candidates,
    )

    result = solve_gate(inputs, gate_kind, horizon_start)

    _last_hint_x.clear()
    _last_hint_x.update({k: 1.0 if v else 0.0 for k, v in result.selected_x.items()})
    _last_hint_q.clear()
    _last_hint_q.update(result.selected_q)

    plan_id = await persist_plan(result.plan_mode, gate_kind, horizon_start, horizon_end, scenarios, result)

    for c in candidates:
        selected = (
            result.selected_x.get(c.opportunity_id, False) or result.selected_q.get(c.opportunity_id, 0.0) > 0
        )
        if selected:
            # Bug fix (combined-deploy pass): this used to key `selected_kw` by the bare interval index
            # `str(t)`, but `opengrid.ledger.reserve()` requires each key to be
            # `encode_interval_key(bank_id, interval_start, interval_end)` -- `decode_interval_key`
            # raises `ValueError` on anything else (confirmed live: every ADMISSION/SCHEDULED_15MIN gate
            # that actually selected a candidate crashed the whole tick with
            # "malformed reservation interval key: '6'"). Also fixes a second, silent bug the old key
            # shape hid: keying by `t` alone collides across banks sharing the same interval index, so a
            # candidate split across two banks would have silently kept only the last bank's amount.
            selected_kw = {
                ledger.encode_interval_key(
                    b,
                    horizon_start + timedelta(minutes=INTERVAL_MINUTES * t),
                    horizon_start + timedelta(minutes=INTERVAL_MINUTES * (t + 1)),
                ): Decimal(str(result.bank_interval_allocation[(c.opportunity_id, b, t)]))
                for t in c.window_intervals
                for b in c.eligible_bank_ids
                if (c.opportunity_id, b, t) in result.bank_interval_allocation
                and result.bank_interval_allocation[(c.opportunity_id, b, t)] > 0
            }
            # Bug fix (combined-deploy pass): `og.reservation.obligation_id` FK-references
            # `og.obligation`, not `og.opportunity` -- `opengrid.contracts.admit`/`admit_priced` mint a
            # fresh `obligation_id` distinct from `opportunity_id` for the OFFERED obligation created
            # alongside the opportunity (`admission.py`). Passing `c.opportunity_id` here raised
            # `ForeignKeyViolation` the moment a candidate was actually selected (confirmed live).
            await ledger.reserve(UUID(c.obligation_id), selected_kw, plan_id)

    return Plan(
        plan_id=plan_id,
        plan_mode=result.plan_mode,
        gate_kind=gate_kind,
        horizon_start=horizon_start,
        horizon_end=horizon_end,
        scenario_set=[{"scenario": s.scenario, "prob": s.probability} for s in scenarios],
        solver_status=result.solver_status,
        solver_gap=Decimal(str(result.solver_gap)) if result.solver_gap is not None else None,
        solver_time_ms=result.solver_time_ms,
        objective_value=Decimal(str(result.objective_value)),
    )


async def _configured_bank_ids() -> tuple[str, ...]:
    """The real bank list, read from `og.bank` (02b S4.2), the fleet topology's single source of truth
    (seeded by `opengrid.fleet.seed` from `integration-sims/config/fleet.yaml`, id scheme `bank-000`..
    `bank-039`). Never fabricated from a count + format guess: an earlier version of this function
    synthesised `f"bank-{i:02d}"` ids (`"bank-01".."bank-NN"`), which are a completely disjoint id space
    from the real `bank-000`-style ids, so every `fleet.capability(bank_id, ...)` call raised
    `LookupError` and `run_gate` never reserved anything (`qa/merge-notes.md` section 11). Overridden by
    tests via `db.load_bank_ids_rows`."""
    bank_ids = await db.load_bank_ids_rows()
    return tuple(bank_ids)
