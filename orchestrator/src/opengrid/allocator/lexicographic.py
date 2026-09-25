"""Lexicographic tier allocation (02a S5.1 S3, S5.2): T1 before T2 before T3 before T4.

MVP-S's allocator never chooses *which* obligations to serve (the selector already committed them);
its job each 2 s tick is to re-derive how much of each already-committed obligation's frozen
`committed_kw` is physically deliverable *right now* from the bank's current capability, honoring
tier priority when capability has shrunk since the commitment was made (e.g. SoC drift).

This is a single-resource (bank capability) allocation, so the two-phase "minimize shortfall, then
maximize economics" LP of `03` S8.4 reduces to a greedy pass in strict tier order: higher tiers are
fully served before a lower tier sees any capability, matching lexicographic optimization exactly
(a greedy allocation of a single scalar resource in strict priority order *is* the lexicographic
optimum -- no LP solver is needed at 2 s cadence). Ties inside a tier are broken by `obligation_id`
for determinism.
"""

from __future__ import annotations

from opengrid.allocator import reasons
from opengrid.allocator.models import TIER_ORDER, ObligationCall, ShortfallReport

_EPS = 1e-9


class TierAllocationResult:
    """Per-obligation granted kW plus any shortfalls, in tier priority order."""

    __slots__ = ("granted_kw", "remaining_capability_kw", "shortfalls")

    def __init__(
        self,
        granted_kw: dict[str, float],
        shortfalls: tuple[ShortfallReport, ...],
        remaining_capability_kw: float,
    ) -> None:
        self.granted_kw = granted_kw
        self.shortfalls = shortfalls
        self.remaining_capability_kw = remaining_capability_kw


def allocate_tiers(
    bank_id: str,
    calls: tuple[ObligationCall, ...],
    capability_kw: float,
    *,
    shortfall_reason: str = reasons.R_COMMIT_LOCK_INFEASIBLE,
) -> TierAllocationResult:
    """S3: serve `calls` for one bank in T1 -> T2 -> T3 -> T4 order, each tier sorted by
    `obligation_id` for a deterministic tie-break. Returns granted kW per obligation (each
    `<= committed_kw`) and a shortfall row for any call that could not be fully served -- reported
    against that same obligation, never satisfied by taking capability from a different one (K13).
    """
    remaining = max(capability_kw, 0.0)
    granted: dict[str, float] = {}
    shortfalls: list[ShortfallReport] = []

    by_tier: dict[str, list[ObligationCall]] = {tier: [] for tier in TIER_ORDER}
    for call in calls:
        by_tier[call.tier].append(call)

    for tier in TIER_ORDER:
        tier_calls = sorted(by_tier[tier], key=lambda c: c.obligation_id)
        for call in tier_calls:
            want = max(call.committed_kw, 0.0)
            give = min(want, remaining)
            granted[call.obligation_id] = give
            remaining -= give
            shortfall_kw = want - give
            if shortfall_kw > _EPS:
                # A committed obligation getting less than its frozen floor is the K13
                # "physical infeasibility" exception -- the bank simply cannot deliver more this
                # cycle -- never a silent reallocation to a different obligation.
                shortfalls.append(
                    ShortfallReport(
                        obligation_id=call.obligation_id,
                        bank_id=bank_id,
                        shortfall_kw=shortfall_kw,
                        reason_code=shortfall_reason,
                    )
                )

    return TierAllocationResult(granted, tuple(shortfalls), max(remaining, 0.0))
