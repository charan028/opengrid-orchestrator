"""Reason-code re-export for the allocator (02a S5).

ALLOC-04: the canonical strings live in `opengrid.core.reasons` (single source of truth shared with
`opengrid.core.limits`, so K13's allowed-exception list matches byte-for-byte); this module only
re-exports the subset the allocator uses, so `opengrid.allocator.reasons.R_...` call sites elsewhere
in this package do not need to change.
"""

from __future__ import annotations

from opengrid.core.reasons import (
    COMMIT_LOCK_OVERRIDE_REASONS,
    R_AS_PARTIAL_DEPLOYMENT,
    R_AS_RELEASE,
    R_BANK_UNAVAILABLE,
    R_COMMIT_LOCK_INFEASIBLE,
    R_COMMIT_LOCK_OVERRIDE_L0,
    R_COMMIT_LOCK_OVERRIDE_L1,
    R_COMMIT_LOCK_OVERRIDE_L2,
    R_COMMIT_LOCK_VIOLATION,
    R_GATEWAY_TIMEOUT,
    R_GRANT_AS_HOLD,
    R_GRANT_CLOSED_LOOP,
    R_GRANT_COMMITTED,
    R_GRANT_DIST_DEFERRAL_PI,
    R_GRANT_HEADROOM,
    R_HUB_VETO_EXCLUDED,
    R_L2_INSTRUCTION_BLOCK,
    R_L2_INSTRUCTION_ESTOP,
    R_L2_INSTRUCTION_LIMIT,
    R_MANUAL_RAMP,
    R_OPERATOR_OVERRIDE,
    R_SHORTFALL_BANK_CAPACITY,
    R_SHORTFALL_L2_INSTRUCTION,
    R_SHORTFALL_NO_SUBSTITUTE,
    R_SUBSTITUTION,
)

__all__ = [
    "COMMIT_LOCK_OVERRIDE_REASONS",
    "R_AS_PARTIAL_DEPLOYMENT",
    "R_AS_RELEASE",
    "R_BANK_UNAVAILABLE",
    "R_COMMIT_LOCK_INFEASIBLE",
    "R_COMMIT_LOCK_OVERRIDE_L0",
    "R_COMMIT_LOCK_OVERRIDE_L1",
    "R_COMMIT_LOCK_OVERRIDE_L2",
    "R_COMMIT_LOCK_VIOLATION",
    "R_GATEWAY_TIMEOUT",
    "R_GRANT_AS_HOLD",
    "R_GRANT_CLOSED_LOOP",
    "R_GRANT_COMMITTED",
    "R_GRANT_DIST_DEFERRAL_PI",
    "R_GRANT_HEADROOM",
    "R_HUB_VETO_EXCLUDED",
    "R_L2_INSTRUCTION_BLOCK",
    "R_L2_INSTRUCTION_ESTOP",
    "R_L2_INSTRUCTION_LIMIT",
    "R_MANUAL_RAMP",
    "R_OPERATOR_OVERRIDE",
    "R_SHORTFALL_BANK_CAPACITY",
    "R_SHORTFALL_L2_INSTRUCTION",
    "R_SHORTFALL_NO_SUBSTITUTE",
    "R_SUBSTITUTION",
]
