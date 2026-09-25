"""Tests for ogsim.market.replay.HistoryReplay against a small synthetic
history TSV (never the real 2.5-day file, so these run anywhere)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from ogsim.market.replay import HistoryReplay

HEADER = "asset\tsignal_type\tunit\treading_time_utc\tvalue\tquality\n"


def write_history(path: Path, rows: list[tuple[str, str, str, str, str, str]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        f.write(HEADER)
        for row in rows:
            f.write("\t".join(row) + "\n")


def test_missing_file_is_reported_as_unavailable(tmp_path: Path):
    replay = HistoryReplay(str(tmp_path / "does-not-exist.tsv"))
    assert replay.available is False


def test_valid_history_file_is_available_and_returns_values(tmp_path: Path):
    path = tmp_path / "history.tsv"
    base = datetime(2026, 9, 1, tzinfo=UTC)
    rows = [
        (
            "SUB1",
            "wholesale_price_mwh",
            "usd_per_mwh",
            (base + timedelta(minutes=i * 15)).strftime("%Y-%m-%d %H:%M:%S"),
            str(20.0 + i),
            "GOOD",
        )
        for i in range(10)
    ]
    write_history(path, rows)
    replay = HistoryReplay(str(path))
    assert replay.available is True
    value = replay.value_at("wholesale_price_mwh", base)
    assert value is not None


def test_unknown_signal_type_returns_none(tmp_path: Path):
    path = tmp_path / "history.tsv"
    base = datetime(2026, 9, 1, tzinfo=UTC)
    write_history(
        path,
        [("SUB1", "wholesale_price_mwh", "usd_per_mwh", base.strftime("%Y-%m-%d %H:%M:%S"), "25.0", "GOOD")],
    )
    replay = HistoryReplay(str(path))
    assert replay.value_at("not_a_real_signal", base) is None


def test_replay_loops_by_wrapping_into_the_history_window(tmp_path: Path):
    path = tmp_path / "history.tsv"
    base = datetime(2026, 9, 1, tzinfo=UTC)
    rows = [
        (
            "SUB1",
            "wholesale_price_mwh",
            "usd_per_mwh",
            (base + timedelta(hours=h)).strftime("%Y-%m-%d %H:%M:%S"),
            str(float(h)),
            "GOOD",
        )
        for h in range(60)  # 2.5 days of hourly data
    ]
    write_history(path, rows)
    replay = HistoryReplay(str(path))
    # Any real "now" maps somewhere inside the (finite) history span.
    virtual_time = replay.virtual_time(datetime.now(UTC))
    assert replay._t0 <= virtual_time < replay._t0 + replay._span_s  # noqa: SLF001 - internal invariant check


def test_malformed_rows_are_skipped_without_raising(tmp_path: Path):
    path = tmp_path / "history.tsv"
    with path.open("w", encoding="utf-8") as f:
        f.write(HEADER)
        f.write("SUB1\twholesale_price_mwh\tusd_per_mwh\tnot-a-date\tnot-a-number\tGOOD\n")
        f.write("SUB1\twholesale_price_mwh\tusd_per_mwh\t2026-09-01 00:00:00\t25.0\tGOOD\n")
    replay = HistoryReplay(str(path))
    assert replay.available is True
