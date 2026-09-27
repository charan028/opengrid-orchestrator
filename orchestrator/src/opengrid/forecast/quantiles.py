"""Pure quantile-persistence math (02b S3, no I/O): same-slot/day-type pooling, the diurnal-profile
fallback for short history, and stale-band widening. Every function here takes plain values in and
returns plain values out so it is exhaustively unit- and property-testable without a database.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import numpy as np

from opengrid.core.timeutil import to_market_tz
from opengrid.forecast.models import FirmFitness

#: 02b S3 "For each of the last 14 days" -- the nominal pool window when enough history exists.
DEFAULT_LOOKBACK_DAYS = 14

#: Task guidance: below this many same-slot/day-type samples, prefer the diurnal-profile fallback
#: over a percentile computed from too few points.
MIN_SLOT_SAMPLES = 3

#: 02b S3 "widened by a fixed multiplier (1.3x)" on a stale slot.
STALE_WIDEN_FACTOR = 1.3

#: 02b S1.4 [forecast] resolution_min default; also the slot bucket width for time-of-day binning.
DEFAULT_RESOLUTION_MIN = 15

_QUANTILE_PCTS = (10.0, 50.0, 90.0)


class InsufficientHistoryError(ValueError):
    """Raised when there is no historical sample at all to forecast from (not even for the
    diurnal fallback) -- distinct from the low-confidence-but-computable case."""


def _slot_of_day(ts_utc: datetime, resolution_min: int) -> int:
    """0-based 15-min (or `resolution_min`) slot index within the market-local day."""
    local = to_market_tz(ts_utc)
    minute_of_day = local.hour * 60 + local.minute
    return minute_of_day // resolution_min


def _day_type(ts_utc: datetime) -> str:
    local = to_market_tz(ts_utc)
    return "weekend" if local.weekday() >= 5 else "weekday"


def same_slot_pool(
    history: list[tuple[datetime, float]],
    target_start_utc: datetime,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    resolution_min: int = DEFAULT_RESOLUTION_MIN,
    any_day_type: bool = False,
) -> list[float]:
    """02b S3's pool: values at the same time-of-day and day-type as `target_start_utc`, drawn from
    up to `lookback_days` of history strictly before it. `history` is `(ts_utc, value)` pairs so this
    module never depends on `FeedObs`/DB shapes -- callers project their rows down to this.

    `any_day_type=True` drops the day-type match (weekday and weekend pooled for the same time-of-day
    slot) -- the short-history relaxation `compute_slot_quantiles` applies before the diurnal fallback."""
    cutoff = target_start_utc - timedelta(days=lookback_days)
    target_slot = _slot_of_day(target_start_utc, resolution_min)
    target_day_type = _day_type(target_start_utc)
    return [
        value
        for ts, value in history
        if cutoff <= ts < target_start_utc
        and _slot_of_day(ts, resolution_min) == target_slot
        and (any_day_type or _day_type(ts) == target_day_type)
    ]


def sample_quantiles(pool: list[float]) -> tuple[float, float, float]:
    """P10/P50/P90 of `pool` via linear-interpolation percentiles (02b S3 "numpy.percentile").
    Raises `InsufficientHistoryError` if `pool` is empty."""
    if not pool:
        raise InsufficientHistoryError("cannot compute sample quantiles from an empty pool")
    p10, p50, p90 = np.percentile(np.asarray(pool, dtype=float), _QUANTILE_PCTS)
    return float(p10), float(p50), float(p90)


def diurnal_fallback_quantiles(
    history: list[tuple[datetime, float]],
    target_start_utc: datetime,
    *,
    resolution_min: int = DEFAULT_RESOLUTION_MIN,
) -> tuple[float, float, float]:
    """Fallback for slots with fewer than `MIN_SLOT_SAMPLES` same-slot/day-type samples (task
    guidance for short history): fit a diurnal profile (mean value per time-of-day slot, pooling
    weekday and weekend together since short history rarely covers both) from *all* available data,
    then take the point estimate for the target slot plus the spread of that profile's residuals
    across all data as the P10/P90 band. Raises `InsufficientHistoryError` if there is no history at
    all to fit from."""
    if not history:
        raise InsufficientHistoryError("cannot fit a diurnal profile from empty history")

    buckets: dict[int, list[float]] = {}
    for ts, value in history:
        buckets.setdefault(_slot_of_day(ts, resolution_min), []).append(value)

    profile = {slot: statistics.fmean(values) for slot, values in buckets.items()}
    overall_mean = statistics.fmean(value for values in buckets.values() for value in values)
    target_slot = _slot_of_day(target_start_utc, resolution_min)
    base = profile.get(target_slot, overall_mean)

    residuals = [value - profile[slot] for slot, values in buckets.items() for value in values]
    if len(residuals) < 2:
        # No spread information available (e.g. a single sample total): a zero-width point estimate
        # is still a valid, if maximally low-confidence, answer -- callers flag NOT_FOR_FIRM regardless.
        return base, base, base

    residual_p10, residual_p90 = np.percentile(np.asarray(residuals, dtype=float), [10.0, 90.0])
    residual_p50 = float(np.median(np.asarray(residuals, dtype=float)))
    return base + float(residual_p10), base + residual_p50, base + float(residual_p90)


def widen(
    quantiles: tuple[float, float, float], factor: float = STALE_WIDEN_FACTOR
) -> tuple[float, float, float]:
    """Scale the P10/P90 distance from P50 by `factor` (02b S3 stale widening). `factor` must be
    positive; ordering (p10 <= p50 <= p90) is preserved since it only rescales already-ordered
    distances from the median."""
    if factor <= 0:
        raise ValueError("widen factor must be positive")
    p10, p50, p90 = quantiles
    return p50 - (p50 - p10) * factor, p50, p50 + (p90 - p50) * factor


#: How a slot's quantiles were derived -- the audit trail for its `firm_fitness`:
#: - `STRICT`: >= `min_slot_samples` same-slot/**same-day-type** samples (the 02b S3 rule) -> `FIRM_OK`.
#: - `POOLED`: too few same-day-type samples, but weekday+weekend pooled for that time-of-day slot had
#:   enough and passed the pooled guard -- firm at weaker confidence -> `FIRM_POOLED` (migration 0040).
#: - `FALLBACK`: the diurnal-profile fallback fired (or the pooled guard failed) -- never firm.
FirmBasis = Literal["STRICT", "POOLED", "FALLBACK"]

#: `og.forecast.firm_fitness` value (and log reason code) for slots firm via the pooled relaxation.
FIRM_POOLED: FirmFitness = "FIRM_POOLED"

#: Pooled guard default (`[forecast].pooled_max_rel_spread`): a pooled slot with NO sample of its own
#: day type is firm only if its (P90 - P10) / |P50| is at most this.
DEFAULT_POOLED_MAX_REL_SPREAD = 0.5


def relative_spread(quantiles: tuple[float, float, float]) -> float:
    """(P90 - P10) / |P50|; infinite for a non-zero spread around a zero median."""
    p10, p50, p90 = quantiles
    spread = p90 - p10
    if p50 == 0:
        return 0.0 if spread == 0 else float("inf")
    return spread / abs(p50)


@dataclass(frozen=True, slots=True)
class SlotQuantiles:
    p10: float
    p50: float
    p90: float
    firm_fitness: FirmFitness
    sample_count: int  # samples in the pool actually used (same-day-type, or pooled when basis=POOLED)
    basis: FirmBasis = "STRICT"


def compute_slot_quantiles(
    history: list[tuple[datetime, float]],
    target_start_utc: datetime,
    *,
    stale: bool,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    resolution_min: int = DEFAULT_RESOLUTION_MIN,
    min_slot_samples: int = MIN_SLOT_SAMPLES,
    widen_factor: float = STALE_WIDEN_FACTOR,
    pool_day_types_when_short: bool = True,
    pooled_max_rel_spread: float | None = DEFAULT_POOLED_MAX_REL_SPREAD,
) -> SlotQuantiles:
    """The full 02b S3 method for one (series, interval) slot: same-slot/day-type pool -> percentile,
    falling back to the diurnal profile below `min_slot_samples`, then widening x`widen_factor` and
    flagging `NOT_FOR_FIRM` if `stale` is set (the caller passes LGV-extended `history` when stale, per
    spec). `NOT_FOR_FIRM` is also set on the fallback path, since a diurnal estimate is inherently
    lower-confidence than a direct same-slot pool.

    Short-history relaxation (`[forecast].pool_day_types_when_short`): when the same-day-type pool is
    short, weekday and weekend samples for the same time-of-day slot are pooled. The slot is firm
    (`FIRM_POOLED`, `basis="POOLED"`) only if that pool reaches `min_slot_samples` AND the guard holds:
    at least one sample of the slot's own day type, OR a pooled relative spread
    (`relative_spread`) <= `pooled_max_rel_spread` (None disables the spread path, leaving only the
    same-day-type-sample path). Otherwise the fallback applies (NOT_FOR_FIRM). The strict rule wins
    automatically as soon as the same-day-type pool alone is large enough."""
    pool = same_slot_pool(
        history, target_start_utc, lookback_days=lookback_days, resolution_min=resolution_min
    )
    basis: FirmBasis = "STRICT"
    if len(pool) < min_slot_samples and pool_day_types_when_short:
        pooled = same_slot_pool(
            history,
            target_start_utc,
            lookback_days=lookback_days,
            resolution_min=resolution_min,
            any_day_type=True,
        )
        if len(pooled) >= min_slot_samples:
            has_own_day_type = len(pool) >= 1
            tight = (
                pooled_max_rel_spread is not None
                and relative_spread(sample_quantiles(pooled)) <= pooled_max_rel_spread
            )
            if has_own_day_type or tight:
                pool, basis = pooled, "POOLED"

    low_confidence = len(pool) < min_slot_samples
    if low_confidence:
        basis = "FALLBACK"
        p10, p50, p90 = diurnal_fallback_quantiles(history, target_start_utc, resolution_min=resolution_min)
    else:
        p10, p50, p90 = sample_quantiles(pool)

    firm_fitness: FirmFitness
    if stale or low_confidence:
        firm_fitness = "NOT_FOR_FIRM"
    else:
        firm_fitness = FIRM_POOLED if basis == "POOLED" else "FIRM_OK"
    if stale:
        p10, p50, p90 = widen((p10, p50, p90), widen_factor)

    return SlotQuantiles(
        p10=p10, p50=p50, p90=p90, firm_fitness=firm_fitness, sample_count=len(pool), basis=basis
    )
