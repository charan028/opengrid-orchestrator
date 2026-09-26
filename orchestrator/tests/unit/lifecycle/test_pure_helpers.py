"""Partition swap bounds, rollup chunking, archive files (write-once, checksum, codec), blob cleanup and
the status renderer -- no database."""

from __future__ import annotations

import gzip
import stat
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from opengrid.lifecycle import archive
from opengrid.lifecycle.archive import (
    ArchiveError,
    ArchiveWriter,
    Manifest,
    archive_paths,
    file_sha256,
    verify_file,
    write_manifest,
)
from opengrid.lifecycle.partitions import PartitionInfo, swap_bounds
from opengrid.lifecycle.retention import TableOutcome, _unlink_blobs
from opengrid.lifecycle.rollup import floor_15, floor_minute, plan_chunks
from opengrid.lifecycle.status import render_status

NOW = datetime(2026, 9, 26, 21, 30, tzinfo=UTC)
D = timedelta(days=1)


def _p(name: str, lower: datetime | None, upper: datetime | None, *, default: bool = False) -> PartitionInfo:
    return PartitionInfo("telemetry", name, default, lower, upper, 0, 0)


def test_swap_bounds_fresh_table_ends_next_midnight() -> None:
    lower, upper = swap_bounds([_p("telemetry_default", None, None, default=True)], NOW)
    assert lower is None
    assert upper == datetime(2026, 9, 27, tzinfo=UTC)


def test_swap_bounds_near_midnight_skips_a_day() -> None:
    late = datetime(2026, 9, 26, 23, 50, tzinfo=UTC)
    _lower, upper = swap_bounds([], late)
    assert upper == datetime(2026, 9, 28, tzinfo=UTC)


def test_swap_bounds_stop_at_first_existing_daily_partition() -> None:
    today = datetime(2026, 9, 26, tzinfo=UTC)
    parts = [
        _p("telemetry_default", None, None, default=True),
        _p("telemetry_p20260920", today - 6 * D, today - 5 * D),
        _p("telemetry_p20260926", today, today + D),
        _p("telemetry_p20260927", today + D, today + 2 * D),
    ]
    lower, upper = swap_bounds(parts, NOW)
    assert lower == today - 5 * D
    assert upper == today


def test_rollup_floors_and_chunks() -> None:
    ts = datetime(2026, 9, 26, 21, 44, 59, 999, tzinfo=UTC)
    assert floor_minute(ts) == datetime(2026, 9, 26, 21, 44, tzinfo=UTC)
    assert floor_15(ts) == datetime(2026, 9, 26, 21, 30, tzinfo=UTC)
    start = datetime(2026, 9, 26, 20, 0, tzinfo=UTC)
    chunks = plan_chunks(start, start + timedelta(minutes=40), timedelta(minutes=15), timedelta(hours=6))
    assert chunks == [
        (start, start + timedelta(minutes=15)),
        (start + timedelta(minutes=15), start + timedelta(minutes=30)),
        (start + timedelta(minutes=30), start + timedelta(minutes=40)),
    ]
    capped = plan_chunks(start, start + timedelta(hours=10), timedelta(hours=1), timedelta(hours=2))
    assert capped[-1][1] == start + timedelta(hours=2)
    assert plan_chunks(start, start, timedelta(minutes=15), timedelta(hours=1)) == []


def test_archive_paths_revisions(tmp_path: Path) -> None:
    data, manifest = archive_paths(tmp_path, "telemetry", date(2026, 9, 20), "zstd", 1)
    assert data == tmp_path / "telemetry" / "2026" / "09" / "20.csv.zst"
    assert manifest == tmp_path / "telemetry" / "2026" / "09" / "20.manifest.json"
    data2, _ = archive_paths(tmp_path, "telemetry", date(2026, 9, 20), "gzip", 2)
    assert data2.name == "20.r2.csv.gz"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_writer_is_write_once_readonly_and_checksummed(tmp_path: Path) -> None:
    target = tmp_path / "t" / "2026" / "09" / "20.csv.gz"
    writer = ArchiveWriter(target, "gzip")
    writer.write(b"hub_id,ts\nh1,2026-09-20T00:00:00Z\n")
    sha, size = writer.finish()
    assert stat.S_IMODE(target.stat().st_mode) == 0o440
    assert not target.with_name(target.name + ".partial").exists()
    assert (sha, size) == file_sha256(target)
    assert gzip.decompress(target.read_bytes()).startswith(b"hub_id,ts\n")
    verify_file(target, sha, size)


def test_verify_detects_tampering(tmp_path: Path) -> None:
    target = tmp_path / "20.csv.gz"
    writer = ArchiveWriter(target, "gzip")
    writer.write(b"a,b\n1,2\n")
    sha, size = writer.finish()
    target.chmod(0o640)
    target.write_bytes(target.read_bytes() + b"x")
    with pytest.raises(ArchiveError, match="checksum mismatch"):
        verify_file(target, sha, size)
    with pytest.raises(ArchiveError, match="missing"):
        verify_file(tmp_path / "nope.csv.gz", sha, size)


def test_writer_abort_leaves_nothing(tmp_path: Path) -> None:
    target = tmp_path / "x.csv.gz"
    writer = ArchiveWriter(target, "gzip")
    writer.write(b"partial")
    writer.abort()
    assert list(tmp_path.iterdir()) == []


def test_manifest_and_header(tmp_path: Path) -> None:
    m = Manifest("feed_obs", "2026-09-20", "/x", "gzip", "csv+header", 3, None, None, "ab", 10, 1, "now")
    path = tmp_path / "20.manifest.json"
    write_manifest(path, m)
    assert '"row_count": 3' in path.read_text(encoding="utf-8")

    class _Reader:
        def __init__(self, data: bytes) -> None:
            self._data, self._i = data, 0

        def read(self, n: int) -> bytes:
            out = self._data[self._i : self._i + n]
            self._i += n
            return out

    reader = _Reader(b'hub_id,"ts",p_kw\r\nh1,2026,1\n')
    assert archive._read_header(reader) == ["hub_id", "ts", "p_kw"]
    assert reader.read(3) == b"h1,"


def test_codec_name_matches_environment() -> None:
    assert archive.codec_name() in ("zstd", "gzip")


def test_unlink_blobs_stays_inside_root(tmp_path: Path) -> None:
    root = tmp_path / "pq"
    blob = root / "2026-09-01" / "hub-1" / "1-a.bin"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"x")
    outside = tmp_path / "secret.bin"
    outside.write_bytes(b"y")
    removed = _unlink_blobs(root, ["2026-09-01/hub-1/1-a.bin", "../secret.bin", "missing.bin"])
    assert removed == 1
    assert not blob.exists() and not (root / "2026-09-01").exists()  # empty dirs pruned
    assert outside.exists()
    assert _unlink_blobs(None, ["a"]) == 0


def test_outcome_and_status_render() -> None:
    outcome = TableOutcome("telemetry", exported_days=2, dropped_partitions=["telemetry_p20260901"])
    assert outcome.as_dict() == {
        "table": "telemetry",
        "exported_days": 2,
        "dropped_partitions": ["telemetry_p20260901"],
    }
    text = render_status(
        {
            "at": NOW.isoformat(),
            "level": "WARN",
            "database_bytes": 14_000_000_000,
            "tables": [
                {
                    "table": "telemetry",
                    "mode": "PARTITION",
                    "keep_days": 7,
                    "legal_hold": False,
                    "protected": False,
                    "bytes": 5_000_000_000,
                    "est_rows": 1234,
                    "partitions": 9,
                    "oldest": "2026-09-20",
                    "next_drop_at": "2026-09-28",
                },
                {"table": "gone", "missing": True},
            ],
            "disks": [{"path": "/srv/pgdata", "used_pct": 81.0, "free_gb": 20.0, "level": "WARN"}],
            "archives": {"files": 3, "bytes": 1_000_000},
            "runs": [{"cycle": "hourly", "started_at": NOW.isoformat(), "ok": True}],
            "run_level": "OK",
        }
    )
    assert "telemetry" in text and "WARN" in text and "(missing)" in text
