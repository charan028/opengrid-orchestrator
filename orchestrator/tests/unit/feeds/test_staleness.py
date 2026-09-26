"""02b S2.6: staleness thresholds and the read-time quality override."""

from __future__ import annotations

from opengrid.feeds.staleness import (
    AS_PRICE_FRESH_S_DEFAULT,
    effective_quality,
    threshold_s_for_product,
)

STALENESS_CFG = {
    "ercot_price_fresh_s": 600,
    "ercot_load_fresh_s": 1800,
    "wind_solar_fresh_s": 10800,
    "nws_fresh_s": 10800,
    "eia_fresh_s": 10800,
}


def test_threshold_by_product() -> None:
    assert threshold_s_for_product("ERCOT", "np6-905-cd", STALENESS_CFG) == 600
    assert threshold_s_for_product("ERCOT", "np6-345-cd", STALENESS_CFG) == 1800
    assert threshold_s_for_product("ERCOT", "np4-732-cd", STALENESS_CFG) == 10800
    assert threshold_s_for_product("ERCOT", "np4-188-cd", STALENESS_CFG) == AS_PRICE_FRESH_S_DEFAULT
    assert threshold_s_for_product("EIA", "eia-demand", STALENESS_CFG) == 10800
    assert threshold_s_for_product("NWS", "nws-hourly", STALENESS_CFG) == 10800


def test_as_price_threshold_is_configurable() -> None:
    """Live defect: `np4-188-cd` (DAM AS clearing prices) used to fall back to a hardcoded constant no
    config key could see or override -- unlike every other product in this table. `as_price_fresh_s`
    must now behave like `ercot_price_fresh_s`/etc: read from `[feeds.staleness]` when present."""
    cfg = {**STALENESS_CFG, "as_price_fresh_s": 43200}
    assert threshold_s_for_product("ERCOT", "np4-188-cd", cfg) == 43200


def test_as_price_threshold_absorbs_dam_publication_gap() -> None:
    """DAM AS prices post once/day (~14:00 CT) for the next delivery day; `last_value_at` is a delivery
    timestamp, so between the current day's last hour and the next post, `now - last_value_at` grows on
    its own by design, observed live to reach ~77 minutes shortly after local midnight and up to roughly
    a full posting cycle just before the next post. The default threshold must comfortably absorb that,
    unlike a short, hourly-style threshold."""
    seventy_seven_minutes = 77 * 60
    assert (
        effective_quality(
            "GOOD",
            source="ERCOT",
            product="np4-188-cd",
            age_s=seventy_seven_minutes,
            staleness_cfg=STALENESS_CFG,
        )
        == "GOOD"
    )


def test_effective_quality_flips_to_stale_past_threshold() -> None:
    assert (
        effective_quality(
            "GOOD", source="ERCOT", product="np6-905-cd", age_s=599, staleness_cfg=STALENESS_CFG
        )
        == "GOOD"
    )
    assert (
        effective_quality(
            "GOOD", source="ERCOT", product="np6-905-cd", age_s=601, staleness_cfg=STALENESS_CFG
        )
        == "STALE"
    )


def test_estimated_also_flips_to_stale() -> None:
    assert (
        effective_quality(
            "ESTIMATED", source="EIA", product="eia-demand", age_s=99999, staleness_cfg=STALENESS_CFG
        )
        == "STALE"
    )
