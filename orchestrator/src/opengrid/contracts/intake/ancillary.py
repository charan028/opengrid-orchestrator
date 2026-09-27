"""ERCOT_AS capacity-hold candidate generation (intake task item "ERCOT_AS"). Pure math -- no I/O.

One capacity-hold candidate per hour of the next operating day, each valued at THAT hour's posted
NP4-188 MCPC for the contract's AS product (the latest posted MCPC only for an hour with none -- issue #43
A1) (`REGUP`/`REGDN`/`RRS`/`NONSPIN`/`ECRS`), quantity per the
contract's product rule (min 0.1 MW / 0.1 MW increment, semi-continuous -- 02a S1.3)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from opengrid.core.products import ProductRule, round_quantity
from opengrid.core.timeutil import MARKET_TZ

#: One ERCOT operating day, hourly granularity (NP4-188-CD is a day-ahead hourly product).
OPERATING_DAY_HOURS = 24

#: Same documented placeholder as `energy.DEFAULT_ENERGY_OFFER_KW` -- MVP-S has no stored per-customer
#: contracted MW; `round_quantity` still enforces the real product rule's min/increment on top of it.
DEFAULT_AS_OFFER_KW = Decimal("500")


@dataclass(frozen=True, slots=True)
class AsCandidate:
    window_start: datetime
    window_end: datetime
    value_per_mwh: Decimal
    requested_kw: Decimal


def next_operating_day_start(now: datetime) -> datetime:
    """Midnight America/Chicago of the next operating day (today's if `now` is still before it,
    tomorrow's otherwise) -- NP4-188-CD prices the *next* operating day, not the current hour."""
    local_now = now.astimezone(MARKET_TZ)
    next_day = (local_now + timedelta(days=1)).date()
    local_midnight = datetime(next_day.year, next_day.month, next_day.day, tzinfo=MARKET_TZ)
    return local_midnight.astimezone(UTC)


def compute_as_candidates(
    *,
    now: datetime,
    mcpc_usd_per_mwh: float,
    rule: ProductRule,
    offer_kw: Decimal = DEFAULT_AS_OFFER_KW,
    hourly_mcpc_usd_per_mwh: Mapping[datetime, float] | None = None,
) -> list[AsCandidate]:
    """One hourly candidate per `OPERATING_DAY_HOURS` starting at the next operating day. NP4-188-CD
    posts one clearing price per product per operating-day HOUR: each candidate is valued at
    `hourly_mcpc_usd_per_mwh[window_start]` (keyed by the hour's UTC start, as `og.feed_obs.ts`
    stores it), and at `mcpc_usd_per_mwh` (the latest posted MCPC) only for an hour with no posted
    value. Returns an empty list if the product rule can never yield a positive quantity
    (`round_quantity` returns 0)."""
    requested_kw = round_quantity(offer_kw, rule, offer_kw)
    if requested_kw <= 0:
        return []
    hourly = hourly_mcpc_usd_per_mwh or {}
    day_start = next_operating_day_start(now)
    candidates = []
    for h in range(OPERATING_DAY_HOURS):
        window_start = day_start + timedelta(hours=h)
        candidates.append(
            AsCandidate(
                window_start=window_start,
                window_end=window_start + timedelta(hours=1),
                value_per_mwh=Decimal(str(hourly.get(window_start, mcpc_usd_per_mwh))),
                requested_kw=requested_kw,
            )
        )
    return candidates
