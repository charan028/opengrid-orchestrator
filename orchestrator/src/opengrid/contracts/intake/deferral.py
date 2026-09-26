"""DIST_DEFERRAL candidate generation (intake task item "DIST_DEFERRAL"). Pure math -- no I/O.

MVP-S's `og.contract` has no delivery-calendar/peak-window columns (02a S1.2's schema is
service-type-generic); rather than guess at a shape no other module reads, this uses one named,
documented peak window (`PEAK_START_HOUR_LOCAL`..`PEAK_END_HOUR_LOCAL`, America/Chicago) as the
contract's calendar for MVP-S -- the next occurrence of that window, one candidate per gate. A real
per-contract calendar (e.g. a `og.contract.delivery_calendar` jsonb column) is a schema follow-up for
whoever owns `og.contract` next, flagged here rather than invented silently (BUILD.md S5a "no silent
fallbacks").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from opengrid.core.products import ProductRule, round_quantity
from opengrid.core.timeutil import MARKET_TZ

PEAK_START_HOUR_LOCAL = 15  # 3pm CT
PEAK_END_HOUR_LOCAL = 19  # 7pm CT

#: Same documented placeholder pattern as `energy.py`/`ancillary.py`.
DEFAULT_DEFERRAL_OFFER_KW = Decimal("500")


@dataclass(frozen=True, slots=True)
class DeferralCandidate:
    window_start: datetime
    window_end: datetime
    requested_kw: Decimal


def _next_peak_window(now: datetime) -> tuple[datetime, datetime]:
    local_now = now.astimezone(MARKET_TZ)
    today_start = local_now.replace(hour=PEAK_START_HOUR_LOCAL, minute=0, second=0, microsecond=0)
    if local_now >= today_start:
        today_start += timedelta(days=1)
    today_end = today_start.replace(hour=PEAK_END_HOUR_LOCAL)
    return today_start.astimezone(UTC), today_end.astimezone(UTC)


def compute_deferral_candidates(
    *, now: datetime, rule: ProductRule, offer_kw: Decimal = DEFAULT_DEFERRAL_OFFER_KW
) -> list[DeferralCandidate]:
    """One firm capacity-hold block for the next peak window, sized per the contract's product rule.
    Empty if the rule can never yield a positive quantity."""
    requested_kw = round_quantity(offer_kw, rule, offer_kw)
    if requested_kw <= 0:
        return []
    window_start, window_end = _next_peak_window(now)
    return [DeferralCandidate(window_start=window_start, window_end=window_end, requested_kw=requested_kw)]
