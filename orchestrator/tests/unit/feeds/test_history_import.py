"""feeds/history_import.py: TSV -> FeedObs mapping (wholesale_price_mwh -> hist-price, substation_load_kw
-> hist-load), idempotent by construction (same natural key as live polling)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from opengrid.feeds.history_import import SOURCE_HIST, iter_feed_obs

TSV_BODY = (
    "asset\tsignal_type\tunit\treading_time_utc\tvalue\tquality\n"
    "HB_NORTH\twholesale_price_mwh\tusd_per_mwh\t2026-09-24T00:00:00+00:00\t34.21\tGOOD\n"
    "SUB_12\tsubstation_load_kw\tkw\t2026-09-24T00:00:00+00:00\t1580.5\tGOOD\n"
    "SYS\tgrid_stress_price_mwh\tusd_per_mwh\t2026-09-24T00:00:00+00:00\t0\tGOOD\n"  # out of scope, skipped
    "HB_NORTH\twholesale_price_mwh\tusd_per_mwh\tnot-a-timestamp\t1\tGOOD\n"  # malformed, skipped
)


def test_iter_feed_obs_maps_and_filters(tmp_path: Path) -> None:
    tsv_path = tmp_path / "history.tsv"
    tsv_path.write_text(TSV_BODY, encoding="utf-8")
    recorded_at = datetime(2026, 9, 26, 0, 0, tzinfo=UTC)

    rows = list(iter_feed_obs(tsv_path, recorded_at=recorded_at))

    assert len(rows) == 2  # the stress-price row and the malformed row are both skipped
    price_row = next(r for r in rows if r.product == "hist-price")
    load_row = next(r for r in rows if r.product == "hist-load")

    assert price_row.source == SOURCE_HIST
    assert price_row.series == "HB_NORTH"
    assert price_row.value == 34.21
    assert price_row.unit == "usd_per_mwh"
    assert price_row.quality == "GOOD"

    assert load_row.series == "SUB_12"
    assert load_row.unit == "kw"
    assert load_row.value == 1580.5


def test_unknown_quality_defaults_to_estimated(tmp_path: Path) -> None:
    tsv_path = tmp_path / "history.tsv"
    tsv_path.write_text(
        "asset\tsignal_type\tunit\treading_time_utc\tvalue\tquality\n"
        "HB_NORTH\twholesale_price_mwh\tusd_per_mwh\t2026-09-24T00:00:00+00:00\t34.21\tWEIRD\n",
        encoding="utf-8",
    )
    rows = list(iter_feed_obs(tsv_path, recorded_at=datetime(2026, 9, 26, tzinfo=UTC)))
    assert rows[0].quality == "ESTIMATED"
