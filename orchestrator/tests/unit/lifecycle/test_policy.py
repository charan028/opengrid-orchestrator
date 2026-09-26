"""Pure lifecycle rules: cutoffs, export eligibility, the whitelist, months, thresholds, config."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from opengrid.lifecycle.policy import (
    BILLING_ARCHIVES,
    DELETABLE_TABLES,
    PARTITIONED_TABLES,
    LifecycleConfig,
    RetentionRow,
    archive_spec,
    can_delete,
    closed_months,
    day_start,
    days_between,
    disk_level,
    drop_cutoff,
    last_exportable_day,
    month_bounds,
    utc_today,
)
from opengrid.platform.config import Config

NOW = datetime(2026, 9, 26, 21, 30, tzinfo=UTC)


def _row(**kw: object) -> RetentionRow:
    base: dict[str, object] = {
        "table_name": "feed_obs",
        "ts_column": "ts",
        "mode": "DELETE",
        "hot_days": None,
        "keep_days": 30,
        "archive": True,
        "legal_hold": False,
        "protected": False,
    }
    base.update(kw)
    return RetentionRow(**base)  # type: ignore[arg-type]


def test_drop_cutoff_keeps_at_least_keep_days() -> None:
    cutoff = drop_cutoff(NOW, 7)
    assert cutoff == datetime(2026, 9, 19, tzinfo=UTC)
    assert NOW - cutoff >= timedelta(days=7)
    assert NOW - cutoff < timedelta(days=8)


def test_utc_today_uses_utc_not_local_offset() -> None:
    chicago_evening = datetime.fromisoformat("2026-09-26T20:00:00-05:00")  # 01:00 UTC on the 27th
    assert utc_today(chicago_evening) == date(2026, 9, 27)


def test_last_exportable_day() -> None:
    assert last_exportable_day(NOW, None) is None
    assert last_exportable_day(NOW, 1) == date(2026, 9, 25)
    assert last_exportable_day(NOW, 0) == date(2026, 9, 25)  # never the open day
    assert last_exportable_day(NOW, 3) == date(2026, 9, 23)


def test_days_between_inclusive_and_empty() -> None:
    assert days_between(date(2026, 9, 1), date(2026, 9, 3)) == [
        date(2026, 9, 1),
        date(2026, 9, 2),
        date(2026, 9, 3),
    ]
    assert days_between(date(2026, 9, 3), date(2026, 9, 1)) == []


def test_can_delete_rules() -> None:
    assert can_delete(_row())
    assert not can_delete(_row(legal_hold=True))
    assert not can_delete(_row(keep_days=None))
    assert not can_delete(_row(protected=True))
    assert not can_delete(_row(mode="NONE"))
    # Billing / audit tables are never deletable even with a (mis)configured policy.
    for table in ("invoice_line", "pnl", "meter_interval", "performance", "trace"):
        assert not can_delete(_row(table_name=table))
        assert table not in DELETABLE_TABLES


def test_partitioned_tables_are_whitelisted() -> None:
    assert set(PARTITIONED_TABLES) <= set(DELETABLE_TABLES)
    assert PARTITIONED_TABLES == {"telemetry": "ts", "telemetry_1m": "bucket"}


def test_archive_specs_resolve() -> None:
    assert archive_spec("verdict").like_table == "verdict"
    assert archive_spec("billing_pnl").like_table == "pnl"
    assert {a.name for a in BILLING_ARCHIVES} == {"billing_invoice_line", "billing_pnl"}
    assert 'og."grant"' in archive_spec("grant").select_sql  # reserved word stays quoted
    with pytest.raises(KeyError):
        archive_spec("invoice_line")


def test_month_bounds_and_closed_months() -> None:
    assert month_bounds(date(2026, 12, 1)) == (day_start(date(2026, 12, 1)), day_start(date(2027, 1, 1)))
    assert closed_months(date(2026, 7, 15), NOW) == [date(2026, 7, 1), date(2026, 8, 1)]
    # September closes only after its grace day.
    assert closed_months(date(2026, 9, 2), datetime(2026, 10, 1, 12, tzinfo=UTC)) == []
    assert closed_months(date(2026, 9, 2), datetime(2026, 10, 2, 0, 1, tzinfo=UTC)) == [date(2026, 9, 1)]


def test_disk_levels() -> None:
    cfg = LifecycleConfig()
    assert disk_level(79.9, cfg) == "OK"
    assert disk_level(80.0, cfg) == "WARN"
    assert disk_level(90.0, cfg) == "CRIT"


def test_config_from_platform_config_with_caps() -> None:
    cfg = Config(
        {
            "lifecycle": {"cold_root": "/srv/ogbackup/cold-x", "batch_rows": 50_000, "days_ahead": 3},
            "pq_ingest": {"blob_store_dir": "/var/lib/opengrid/pq_waveform"},
        }
    )
    lcfg = LifecycleConfig.from_config(cfg)
    assert lcfg.cold_root == Path("/srv/ogbackup/cold-x")
    assert lcfg.batch_rows == 10_000  # never above 10k rows per statement
    assert lcfg.days_ahead == 3
    assert lcfg.pq_blob_dir == Path("/var/lib/opengrid/pq_waveform")
    assert LifecycleConfig.from_config(Config({})).pq_blob_dir is None
