"""Staleness thresholds and read-time quality override (02b S2.6).

`og.feed_status`/`og.feed_obs` (architect schema, `migrations/0001_init.sql`) store age-relevant
timestamps but not a live "is this stale right now" flag -- per 02b S2.6 "age_s computed on read, not
stored". This module is the single place that turns `(product, age_s)` into an effective quality, so
`latest()`/`window()` and the scheduler's degraded-mode tracing agree on the same thresholds
(`opengrid.core.timeutil.is_stale` does the actual age comparison; owned there, used here).
"""

from __future__ import annotations

# 02a/02b do not define an AS-price-specific staleness budget (NP4-188-CD posts once/day at 14:00 CT);
# a named constant with a generous multiple of the posting cadence stands in for it.
AS_PRICE_FRESH_S = 26 * 3600

_PRODUCT_THRESHOLD_KEY = {
    "np6-905-cd": "ercot_price_fresh_s",
    "np6-345-cd": "ercot_load_fresh_s",
    "np4-732-cd": "wind_solar_fresh_s",
    "np4-737-cd": "wind_solar_fresh_s",
    "np4-188-cd": None,  # AS_PRICE_FRESH_S, below
}


def threshold_s_for_product(source: str, product: str, staleness_cfg: dict[str, float]) -> float:
    """Resolve the `[feeds.staleness]` threshold (seconds) for a given source/product."""
    if source == "EIA":
        return float(staleness_cfg.get("eia_fresh_s", 10800))
    if source == "NWS":
        return float(staleness_cfg.get("nws_fresh_s", 10800))
    key = _PRODUCT_THRESHOLD_KEY.get(product)
    if key is None:
        return float(AS_PRICE_FRESH_S)
    return float(staleness_cfg.get(key, 600))


def effective_quality(
    base_quality: str, *, source: str, product: str, age_s: float, staleness_cfg: dict[str, float]
) -> str:
    """A value past its staleness threshold reads as `STALE` regardless of its stored quality (a
    GOOD or ESTIMATED reading both become STALE once too old -- 02b S2.6)."""
    threshold_s = threshold_s_for_product(source, product, staleness_cfg)
    return "STALE" if age_s > threshold_s else base_quality
