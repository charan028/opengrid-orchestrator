"""Substitution within an obligation (02a S5.3, S5.5): re-home an obligation's granted kW onto other
eligible hubs of the SAME obligation when one or more of its currently-assigned hubs are unhealthy.

This module never reduces an obligation's granted total on its own initiative and never reassigns kW
to a different obligation_id -- doing either would violate K13. If the obligation's healthy eligible
hubs cannot physically deliver the granted amount, the shortfall is reported against that obligation
with `R-COMMIT-LOCK-INFEASIBLE` (the one lock exception that fits: no substitute exists) and the
returned per-hub allocation is truncated to what is actually deliverable -- never invented.
"""

from __future__ import annotations

from opengrid.allocator import reasons
from opengrid.allocator.models import HubSnapshot, ShortfallReport, SubstitutionEvent
from opengrid.allocator.waterfill import water_fill

_EPS = 1e-9


class SubstitutionResult:
    __slots__ = ("event", "per_hub_kw", "shortfall")

    def __init__(
        self,
        per_hub_kw: dict[str, float],
        shortfall: ShortfallReport | None,
        event: SubstitutionEvent | None,
    ) -> None:
        self.per_hub_kw = per_hub_kw
        self.shortfall = shortfall
        self.event = event


def realize_obligation(
    obligation_id: str,
    bank_id: str,
    eligible_hubs: tuple[HubSnapshot, ...],
    needed_kw: float,
    *,
    stickiness: float = 0.2,
) -> SubstitutionResult:
    """Water-fill `needed_kw` across `eligible_hubs`, substituting away any unhealthy hub. Returns
    the realized per-hub allocation (S4/S5), a shortfall row if the healthy set can't cover
    `needed_kw` (S5.3's `R-COMMIT-LOCK-INFEASIBLE`), and a substitution event when unhealthy hubs
    were excluded and at least one healthy hub was used instead (S5, traced with `R-SUBSTITUTION`).
    """
    if needed_kw <= _EPS:
        return SubstitutionResult({}, None, None)

    healthy = tuple(sorted((h for h in eligible_hubs if h.is_healthy), key=lambda h: h.hub_id))
    unhealthy_ids = tuple(sorted(h.hub_id for h in eligible_hubs if not h.is_healthy))

    available = sum(h.free_discharge_kw for h in healthy)
    deliverable = min(needed_kw, available)

    per_hub = water_fill(healthy, deliverable, stickiness=stickiness)

    shortfall = None
    if deliverable < needed_kw - _EPS:
        shortfall = ShortfallReport(
            obligation_id=obligation_id,
            bank_id=bank_id,
            shortfall_kw=needed_kw - deliverable,
            reason_code=reasons.R_COMMIT_LOCK_INFEASIBLE,
        )

    event = None
    if unhealthy_ids:
        used_ids = tuple(sorted(hid for hid, kw in per_hub.items() if kw > _EPS))
        event = SubstitutionEvent(
            obligation_id=obligation_id,
            bank_id=bank_id,
            from_hub_ids=unhealthy_ids,
            to_hub_ids=used_ids,
        )

    return SubstitutionResult(per_hub, shortfall, event)
