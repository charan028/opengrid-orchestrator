"""Staleness thresholds and read-time quality override (02b S2.6).

`og.feed_status`/`og.feed_obs` (architect schema, `migrations/0001_init.sql`) store age-relevant
timestamps but not a live "is this stale right now" flag -- per 02b S2.6 "age_s computed on read, not
stored". This module is the single place that turns `(product, age_s)` into an effective quality, so
`latest()`/`window()` and the scheduler's degraded-mode tracing agree on the same thresholds
(`opengrid.core.timeutil.is_stale` does the actual age comparison; owned there, used here).
"""

from __future__ import annotations

# NP4-188-CD (DAM AS clearing prices) posts once/day (~14:00 CT) for the *next* delivery day, and
# `last_value_at` is a delivery-interval timestamp, not a publish timestamp -- so between the last hour
# of the current DAM day and the next post, `now - last_value_at` grows on its own, by design, up to
# roughly the length of one posting cycle, before the next post pulls it back to (usually) negative
# again (live defect: confirmed via a live probe that this is exactly what produced a "stale for 77
# minutes" false alarm shortly after local midnight -- the feed itself was healthy: 200 OK, 360 fresh
# rows across all 5 AS products, the latest already covering the rest of that DAM day). A named default
# with a generous multiple of the ~24h posting cadence absorbs that normal swing; configurable via
# `feeds.staleness.as_price_fresh_s` like every other product here, rather than a hard constant no one
# else can see or tune.
AS_PRICE_FRESH_S_DEFAULT = 26 * 3600

_PRODUCT_THRESHOLD_KEY = {
    "np6-905-cd": "ercot_price_fresh_s",
    "np6-345-cd": "ercot_load_fresh_s",
    "np4-732-cd": "wind_solar_fresh_s",
    "np4-737-cd": "wind_solar_fresh_s",
    "np4-188-cd": "as_price_fresh_s",
}

_PRODUCT_THRESHOLD_DEFAULT = {
    "np6-905-cd": 600.0,
    "np6-345-cd": 600.0,
    "np4-732-cd": 600.0,
    "np4-737-cd": 600.0,
    "np4-188-cd": float(AS_PRICE_FRESH_S_DEFAULT),
}


def threshold_s_for_product(source: str, product: str, staleness_cfg: dict[str, float]) -> float:
    """Resolve the `[feeds.staleness]` threshold (seconds) for a given source/product."""
    if source == "EIA":
        return float(staleness_cfg.get("eia_fresh_s", 10800))
    if source == "NWS":
        return float(staleness_cfg.get("nws_fresh_s", 10800))
    key = _PRODUCT_THRESHOLD_KEY.get(product)
    if key is None:
        return 600.0
    return float(staleness_cfg.get(key, _PRODUCT_THRESHOLD_DEFAULT[product]))


def effective_quality(
    base_quality: str, *, source: str, product: str, age_s: float, staleness_cfg: dict[str, float]
) -> str:
    """A value past its staleness threshold reads as `STALE` regardless of its stored quality (a
    GOOD or ESTIMATED reading both become STALE once too old -- 02b S2.6)."""
    threshold_s = threshold_s_for_product(source, product, staleness_cfg)
    return "STALE" if age_s > threshold_s else base_quality
