"""Canonical reason-code constants (02a S2.1, S5; 00-invariants.md K13).

Single source of truth for every reason code the engine writes to `og.grant`/`og.commitment`/
`og.trace` or checks against. `opengrid.core.limits.check_commitment_lock`'s allowed-override set and
`opengrid.allocator`'s grant/shortfall reason codes must use the exact same strings -- ALLOC-04's
"canonical reason codes ... used by core.limits and allocator (delete the duplicated literals)".
Neither module may define its own copy of any of these; `opengrid.allocator.reasons` re-exports this
module rather than redeclaring the strings.
"""

from __future__ import annotations

# --- K13 commitment-lock exceptions (00-invariants.md K13; 02a S2.1) -----------------------------
R_COMMIT_LOCK_OVERRIDE_L0 = "R-COMMIT-LOCK-OVERRIDE-L0"
R_COMMIT_LOCK_OVERRIDE_L1 = "R-COMMIT-LOCK-OVERRIDE-L1"
R_COMMIT_LOCK_OVERRIDE_L2 = "R-COMMIT-LOCK-OVERRIDE-L2"
R_COMMIT_LOCK_INFEASIBLE = "R-COMMIT-LOCK-INFEASIBLE"
R_AS_RELEASE = "R-AS-RELEASE"

#: Allowed for a grant to reduce below an obligation's committed floor (02a S2.1's four-path list).
COMMIT_LOCK_OVERRIDE_REASONS = frozenset(
    {
        R_COMMIT_LOCK_OVERRIDE_L0,
        R_COMMIT_LOCK_OVERRIDE_L1,
        R_COMMIT_LOCK_OVERRIDE_L2,
        R_COMMIT_LOCK_INFEASIBLE,
    }
)

#: A commitment-lock violation that matched none of the allowed reasons above.
R_COMMIT_LOCK_VIOLATION = "R-COMMIT-LOCK-VIOLATION"

# --- Hub-level realization change, never a commitment write (02a S5.3, K13 exception list) -------
R_SUBSTITUTION = "R-SUBSTITUTION"

# --- Normal grant reasons (not shortfalls, not overrides) -----------------------------------------
R_GRANT_COMMITTED = "R-GRANT-COMMITTED"
R_GRANT_HEADROOM = "R-GRANT-HEADROOM"
R_GRANT_DIST_DEFERRAL_PI = "R-GRANT-DIST-DEFERRAL-PI"
#: Need-basis delivery (00-invariants.md K13, owner decision 2026-09-26): a measured closed-loop profile's
#: grant follows the customer's measured need below its reserved maximum; the reservation stays locked.
R_GRANT_CLOSED_LOOP = "R-GRANT-CLOSED-LOOP"

# --- Shortfall reasons reported against the obligation that could not be fully served (never a
# reallocation to a different obligation -- 00-invariants.md K13) ---------------------------------
R_SHORTFALL_BANK_CAPACITY = "R-SHORTFALL-BANK-CAPACITY"
R_SHORTFALL_NO_SUBSTITUTE = "R-SHORTFALL-NO-SUBSTITUTE"
R_SHORTFALL_L2_INSTRUCTION = "R-SHORTFALL-L2-INSTRUCTION"

#: A best-effort partial grant after a mid-window SHORTFALL (owner decision 2026-09-26) carries the shortfall
#: reason; this is the K13 lock-exception it stands for. One copy, for the engine's escalation and the
#: guardian's G-19 corroboration alike.
LOCK_REASON_BY_SHORTFALL: dict[str, str] = {
    R_SHORTFALL_L2_INSTRUCTION: R_COMMIT_LOCK_OVERRIDE_L2,
    R_SHORTFALL_NO_SUBSTITUTE: R_COMMIT_LOCK_INFEASIBLE,
    R_SHORTFALL_BANK_CAPACITY: R_COMMIT_LOCK_INFEASIBLE,
}

# --- L2 grid-authority instruction handling (K5) --------------------------------------------------
R_L2_INSTRUCTION_LIMIT = "R-L2-INSTRUCTION-LIMIT"
R_L2_INSTRUCTION_BLOCK = "R-L2-INSTRUCTION-BLOCK"
R_L2_INSTRUCTION_ESTOP = "R-L2-INSTRUCTION-ESTOP"

# --- Planning/signing-time envelope-check failures (core.limits' G-01..G-06/K2 checks) ------------
R_RESERVE_FLOOR = "RESERVE_FLOOR"
R_HUB_POWER_LIMIT = "HUB_POWER_LIMIT"
R_BANK_KVA_LIMIT = "BANK_KVA_LIMIT"
R_HUB_RAMP_LIMIT = "HUB_RAMP_LIMIT"
R_FLEET_RAMP_CAP = "FLEET_RAMP_CAP"
R_FEEDER_RAMP_CEILING = "FEEDER_RAMP_CEILING"
R_ONE_BUYER_EXCEEDED = "ONE_BUYER_EXCEEDED"

# --- G-01-ENERGY: lease-duration energy projection (K1), independent of the instantaneous G-01 check
R_RESERVE_FLOOR_LEASE = "RESERVE_FLOOR_LEASE"
R_CHARGE_CEILING_LEASE = "CHARGE_CEILING_LEASE"

# --- Continuous per-obligation energy-sufficiency check (K1, 03-decision-engine.md S8.11) ----------
R_ENERGY_SOC_MISSING = "R-ENERGY-SOC-MISSING"
ALR_ENERGY_SHORTFALL_RISK = "ALR-ENERGY-SHORTFALL-RISK"

# --- Gateway/dependency timeout handling (ALLOC-05: allocator run_cycle) --------------------------
R_GATEWAY_TIMEOUT = "R-GATEWAY-TIMEOUT"
