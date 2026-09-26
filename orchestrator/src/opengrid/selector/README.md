# opengrid.selector

Mode O LP/MILP gate runner (`02a-mvp-s-spec-engine.md` §3). Owner: selector agent (BUILD.md §4).

## Purpose

Runs the selector gate every 15 minutes plus on admission/re-nomination events: loads frozen
commitments (K13/C24) and candidate opportunities, builds a HiGHS MILP over banks x 96 intervals x
{P10,P50,P90} scenarios, solves it with a time limit and warm start, independently re-validates the
solution, and falls back to a deterministic rule selector (F2) on infeasibility, timeout-without-gap,
or a failed validation. F2 also serves as the KPI-22 rule-baseline.

## Public interface (fixed, see `orchestrator/INTERFACES.md`)

- `run_gate(gate_kind, contract_scope=None) -> Plan`
- `load_frozen_commitments(horizon_start, horizon_end) -> dict[UUID, dict[str, float]]`

## Submodules

| Module | Role |
|---|---|
| `types` | Pure dataclasses shared by every piece below (no I/O). |
| `model` | Pure function: `ModelInputs` -> a highspy `Highs` model (`build_mode_o_model`). |
| `solve` | Runs the model (two lexicographic stages when a regulated candidate exists, 09 D3); recovers the price-of-firmness and stored-energy duals (`highs_solve`). |
| `extract` | Turns a solved model into an `ExtractedPlan` (`extract_plan`). |
| `validate` | Independent re-derivation of K1/K2/K13/product-rule constraints from raw numbers (`validate_plan`). |
| `rule_fallback` | F2: firm-then-AS-then-market greedy selector; also the KPI-22 baseline, run on every gate (`rule_fallback_f2`). |
| `value` | One net-value evaluator for the LP and rule plans (wear via `core.economics.wear_cost`, M1), and the ES05-S07 shadow comparison. |
| `energy_value` | 09 D7 stored-energy value and the hard hold floor per bank/interval; the DISPATCH read API (`discharge_threshold_usd_per_mwh`, `hold_floor_kwh`). |
| `solar_history` | D-28 measured solar share of charging: trailing 7-day same-hour share per zone from fleet telemetry, via `core.solar_share`. |
| `db` | Selector's read-only queries against `og.commitment` et al., and its own analytics tables (migration 0030). |
| `gate` | Orchestration: `run_gate`/`solve_gate`, the horizon/loader plumbing, market terms and K15 territory via `opengrid.market.MarketModel`, `persist_plan`. |

## Economics (09 S1.5)

- Each bank is priced at its own load zone's forecast (`ScenarioPrice.price_by_bank`; load rows ignored).
- Wear (D8) on every discharged kWh -- deliveries and headroom alike -- at the bank's asset-class rate;
  never on holds (AS awards, regulated need-basis reserves) or charging.
- Charging: zone price + the TDSP's M1 in the ERCOT competitive area; the utility's own charging terms
  (TOU, solar floor) in a regulated territory, no M1. Exports never recover M1.
- Regulated candidates are selected first (stage R), then the full net value on the remainder (stage F).
- Charging is split solar / grid (C27): solar availability = the D-28 measured share x the charge
  envelope; a regulated bank has a 30% soft solar floor and charges from the grid only at night/off-peak.
- No wash trade: no charging while delivering a regulated obligation (C7(b)'); grid charging and headroom
  sale share one envelope per interval.
- Held AS awards pay wear on their expected deployment (psi, an assumption: 2%).

## Known gaps (see the build's final report for detail)

- Charge-side constraints C8/C9/C14/C18, home load (F2) and the flow families F1-F7 (DISPATCH and the
  guardian enforce F1-F7) are not modelled in the selector.

## How to test

```
.venv\Scripts\python.exe -m pytest orchestrator\tests\unit\selector -q --cov=opengrid.selector
.venv\Scripts\python.exe -m ruff check orchestrator\src\opengrid\selector orchestrator\tests\unit\selector
.venv\Scripts\python.exe -m ruff format --check orchestrator\src\opengrid\selector orchestrator\tests\unit\selector
.venv\Scripts\python.exe -m mypy orchestrator\src\opengrid\selector
```

No DB/MQTT required; `fleet`/`forecast`/`contracts`/`ledger` are faked in tests (BUILD.md "use fakes for
siblings").
