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
| `solve` | Runs the model with a time limit/warm start; recovers price-of-firmness duals (`highs_solve`). |
| `extract` | Turns a solved model into an `ExtractedPlan` (`extract_plan`). |
| `validate` | Independent re-derivation of K1/K2/K13/product-rule constraints from raw numbers (`validate_plan`). |
| `rule_fallback` | F2: firm-then-AS-then-market greedy selector; also the KPI-22 baseline (`rule_fallback_f2`). |
| `db` | Selector's own read-only queries against `og.commitment` et al. |
| `gate` | Orchestration: `run_gate`/`solve_gate`, the horizon/loader plumbing, `persist_plan`. |

## Known gaps (see the build's final report for detail)

- Full physics (SoC dynamics, charge-side constraints C7-C9/C14/C15/C18) is deliberately left on the
  `fleet.capability`/`forecast.scenarios` side of the interface boundary -- see `model.py`'s module
  docstring.
- `gate.load_candidates`/`load_committed`'s bank-eligibility and `_configured_bank_ids` are placeholders
  until `contracts` exposes an opportunity-listing query and the platform config schema defines
  `[banks]` (both outside this package's ownership).
- `db.py`/`gate.persist_plan` are correct against the `02a` §1 DDL but unexercised against a live
  database locally (no migrations exist yet, per BUILD.md §5's architect-owned `migrations/`).

## How to test

```
.venv\Scripts\python.exe -m pytest orchestrator\tests\unit\selector -q --cov=opengrid.selector
.venv\Scripts\python.exe -m ruff check orchestrator\src\opengrid\selector orchestrator\tests\unit\selector
.venv\Scripts\python.exe -m ruff format --check orchestrator\src\opengrid\selector orchestrator\tests\unit\selector
.venv\Scripts\python.exe -m mypy orchestrator\src\opengrid\selector
```

No DB/MQTT required; `fleet`/`forecast`/`contracts`/`ledger` are faked in tests (BUILD.md "use fakes for
siblings").
