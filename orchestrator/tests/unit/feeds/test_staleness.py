"""02b S2.6: staleness thresholds and the read-time quality override."""

from __future__ import annotations

from opengrid.feeds.staleness import AS_PRICE_FRESH_S, effective_quality, threshold_s_for_product

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
    assert threshold_s_for_product("ERCOT", "np4-188-cd", STALENESS_CFG) == AS_PRICE_FRESH_S
    assert threshold_s_for_product("EIA", "eia-demand", STALENESS_CFG) == 10800
    assert threshold_s_for_product("NWS", "nws-hourly", STALENESS_CFG) == 10800


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
