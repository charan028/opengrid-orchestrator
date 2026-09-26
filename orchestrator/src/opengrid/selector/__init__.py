"""opengrid.selector -- Mode O LP/MILP gate runner (02a S3). Owner: selector agent (BUILD.md S4).

Implements `run_gate` per 02a S3.8's pseudocode: loads frozen commitments (K13/C24), builds the HiGHS
model (`model.build_mode_o_model`), solves it with a time limit and warm start (`solve.highs_solve`),
independently re-validates the solution (`validate.validate_plan`), falls back to the rule selector
(`rule_fallback.rule_fallback_f2`) on infeasibility/timeout/gap/validation failure, and transitions
opportunities through `contracts`/`ledger` (`gate.run_gate`).

Submodules:
    types           -- pure dataclasses shared by every piece below (no I/O).
    model           -- pure function: `ModelInputs` -> a highspy `Highs` model.
    solve           -- runs the model with a time limit/warm start; recovers price-of-firmness duals.
    extract         -- turns a solved model into an `ExtractedPlan`.
    validate        -- independent re-derivation of K1/K2/K13/product-rule constraints from raw numbers.
    rule_fallback   -- F2: firm-then-AS-then-market greedy selector; also the KPI-22 baseline.
    value           -- one net-value evaluator for LP and rule plans; the ES05-S07 shadow comparison.
    energy_value    -- 09 D7 stored-energy value per bank/interval; DISPATCH's read API.
    db              -- selector's read-only queries (`og.commitment` et al.) and its analytics tables.
    gate            -- orchestration: `run_gate`/`solve_gate`, the horizon/loader plumbing.
"""

from __future__ import annotations

from opengrid.selector.db import load_frozen_commitments
from opengrid.selector.gate import run_gate, solve_gate

__all__ = ["load_frozen_commitments", "run_gate", "solve_gate"]
