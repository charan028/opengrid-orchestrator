"""Reason-code constants for the allocator (02a S5). Single source of truth so every module -- and
every test that asserts on a reason code -- uses the exact same string (K13's allowed-exception list
must match `opengrid.core.limits.check_commitment_lock` byte-for-byte).
"""

from __future__ import annotations

# K13 commitment-lock exceptions (00-invariants.md K13; 02a S2.1).
R_COMMIT_LOCK_OVERRIDE_L0 = "R-COMMIT-LOCK-OVERRIDE-L0"
R_COMMIT_LOCK_OVERRIDE_L1 = "R-COMMIT-LOCK-OVERRIDE-L1"
R_COMMIT_LOCK_OVERRIDE_L2 = "R-COMMIT-LOCK-OVERRIDE-L2"
R_COMMIT_LOCK_INFEASIBLE = "R-COMMIT-LOCK-INFEASIBLE"
R_AS_RELEASE = "R-AS-RELEASE"

# Allowed for a grant to reduce below an obligation's committed floor (02a S2.1's four-path list).
COMMIT_LOCK_OVERRIDE_REASONS = frozenset(
    {
        R_COMMIT_LOCK_OVERRIDE_L0,
        R_COMMIT_LOCK_OVERRIDE_L1,
        R_COMMIT_LOCK_OVERRIDE_L2,
        R_COMMIT_LOCK_INFEASIBLE,
    }
)

# Hub-level realization change, never a commitment write (02a S5.3, K13 exception list).
R_SUBSTITUTION = "R-SUBSTITUTION"

# Normal grant reasons (not shortfalls, not overrides).
R_GRANT_COMMITTED = "R-GRANT-COMMITTED"
R_GRANT_HEADROOM = "R-GRANT-HEADROOM"
R_GRANT_DIST_DEFERRAL_PI = "R-GRANT-DIST-DEFERRAL-PI"

# Shortfall reasons reported against the obligation that could not be fully served (never a
# reallocation to a different obligation -- 00-invariants.md K13).
R_SHORTFALL_BANK_CAPACITY = "R-SHORTFALL-BANK-CAPACITY"
R_SHORTFALL_NO_SUBSTITUTE = "R-SHORTFALL-NO-SUBSTITUTE"
R_SHORTFALL_L2_INSTRUCTION = "R-SHORTFALL-L2-INSTRUCTION"

# L2 grid-authority instruction handling (K5).
R_L2_INSTRUCTION_LIMIT = "R-L2-INSTRUCTION-LIMIT"
R_L2_INSTRUCTION_BLOCK = "R-L2-INSTRUCTION-BLOCK"
R_L2_INSTRUCTION_ESTOP = "R-L2-INSTRUCTION-ESTOP"
