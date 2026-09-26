"""opengrid.pq_ingest.pg_backend.PgPqIngestBackend against a minimal fake psycopg
pool/cursor (mirrors tests/unit/engine/test_gateways.py's pattern) -- no real DB."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from opengrid.core.models.pq import PqWaveformRawIndex, PqWaveformSummaryRow
from opengrid.pq_ingest.characterize import HubCharacterization
from opengrid.pq_ingest.pg_backend import PgPqIngestBackend

pytestmark = pytest.mark.asyncio


class FakeCursor:
    def __init__(self, responses: list | None = None) -> None:
        self._responses = list(responses or [])
        self.executed: list[tuple[str, dict]] = []
        self.executed_many: list[tuple[str, list[dict]]] = []
        self._current = None

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._current = self._responses.pop(0) if self._responses else None

    async def executemany(self, sql, params_seq):
        self.executed_many.append((sql, list(params_seq)))

    async def fetchone(self):
        return self._current

    async def fetchall(self):
        return self._current or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, cursor: FakeCursor) -> None:
        self._conn = FakeConn(cursor)

    def connection(self):
        return self._conn


async def test_insert_summary_executes_and_commits() -> None:
    cursor = FakeCursor()
    pool = FakePool(cursor)
    backend = PgPqIngestBackend(pool)  # type: ignore[arg-type]
    row = PqWaveformSummaryRow(
        hub_id="hub-00000", ts=datetime(2026, 9, 26, tzinfo=UTC), v_rms_a=240.0, freq_hz=60.0
    )

    await backend.insert_summary(row)

    assert len(cursor.executed) == 1
    sql, params = cursor.executed[0]
    assert "INSERT INTO og.pq_waveform_summary" in sql
    assert params["hub_id"] == "hub-00000"
    assert pool._conn.committed is True


async def test_insert_summary_wraps_harmonics_as_jsonb() -> None:
    from psycopg.types.json import Jsonb

    cursor = FakeCursor()
    pool = FakePool(cursor)
    backend = PgPqIngestBackend(pool)  # type: ignore[arg-type]
    row = PqWaveformSummaryRow(
        hub_id="hub-00000",
        ts=datetime(2026, 9, 26, tzinfo=UTC),
        harmonics_i={"3": {"mag_pct": 1.2, "angle_deg": 30.0}},
    )

    await backend.insert_summary(row)

    _, params = cursor.executed[0]
    assert isinstance(params["harmonics_i"], Jsonb)
    assert params["harmonics_v"] is None


async def test_insert_summaries_batch_executes_one_executemany_and_commits() -> None:
    cursor = FakeCursor()
    pool = FakePool(cursor)
    backend = PgPqIngestBackend(pool)  # type: ignore[arg-type]
    rows = [
        PqWaveformSummaryRow(hub_id=f"hub-{i:05d}", ts=datetime(2026, 9, 26, tzinfo=UTC), freq_hz=60.0)
        for i in range(3)
    ]

    await backend.insert_summaries_batch(rows)

    assert any("SET LOCAL synchronous_commit" in sql for sql, _ in cursor.executed)
    assert len(cursor.executed_many) == 1
    sql, params_list = cursor.executed_many[0]
    assert "INSERT INTO og.pq_waveform_summary" in sql
    assert [p["hub_id"] for p in params_list] == ["hub-00000", "hub-00001", "hub-00002"]
    assert pool._conn.committed is True


async def test_insert_summaries_batch_empty_is_a_noop() -> None:
    cursor = FakeCursor()
    pool = FakePool(cursor)
    backend = PgPqIngestBackend(pool)  # type: ignore[arg-type]

    await backend.insert_summaries_batch([])

    assert cursor.executed == []
    assert pool._conn.committed is False


async def test_upsert_hub_inverter_pq_batch_executes_one_executemany_and_commits() -> None:
    cursor = FakeCursor()
    pool = FakePool(cursor)
    backend = PgPqIngestBackend(pool)  # type: ignore[arg-type]
    rows = [
        HubCharacterization(
            hub_id=f"hub-{i:05d}",
            phase_connection="A",
            kva_rating=11.6,
            freq_offset_hz=0.01,
            freq_offset_std_hz=0.005,
            voltage_offset_pct=0.3,
            voltage_offset_std_pct=0.1,
            thd_current_pct=2.0,
            dominant_harmonics=None,
            phase_angle_error_deg=0.5,
            quality_score=0.9,
            last_estimated_at=datetime(2026, 9, 27, tzinfo=UTC),
        )
        for i in range(3)
    ]

    await backend.upsert_hub_inverter_pq_batch(rows)

    assert any("SET LOCAL synchronous_commit" in sql for sql, _ in cursor.executed)
    assert len(cursor.executed_many) == 1
    sql, params_list = cursor.executed_many[0]
    assert "INSERT INTO og.hub_inverter_pq" in sql
    assert "ON CONFLICT (hub_id) DO UPDATE" in sql
    assert [p["hub_id"] for p in params_list] == ["hub-00000", "hub-00001", "hub-00002"]
    assert pool._conn.committed is True
    # Nameplate/asset-health columns are never touched by this upsert (see the SQL's own
    # comment) -- they must not appear in the parameter dict at all.
    for params in params_list:
        assert "pf_min_leading" not in params
        assert "response_time_ms" not in params
        assert "ride_through_class" not in params
        assert "asset_state" not in params


async def test_upsert_hub_inverter_pq_batch_wraps_harmonics_as_jsonb() -> None:
    from psycopg.types.json import Jsonb

    cursor = FakeCursor()
    pool = FakePool(cursor)
    backend = PgPqIngestBackend(pool)  # type: ignore[arg-type]
    row = HubCharacterization(
        hub_id="hub-00000",
        phase_connection="A",
        kva_rating=11.6,
        freq_offset_hz=0.0,
        freq_offset_std_hz=0.0,
        voltage_offset_pct=0.0,
        voltage_offset_std_pct=0.0,
        thd_current_pct=2.0,
        dominant_harmonics={"3": {"mag_pct": 1.2, "angle_deg": 30.0}},
        phase_angle_error_deg=0.0,
        quality_score=1.0,
        last_estimated_at=datetime(2026, 9, 27, tzinfo=UTC),
    )

    await backend.upsert_hub_inverter_pq_batch([row])

    _, params_list = cursor.executed_many[0]
    assert isinstance(params_list[0]["dominant_harmonics"], Jsonb)


async def test_upsert_hub_inverter_pq_batch_empty_is_a_noop() -> None:
    cursor = FakeCursor()
    pool = FakePool(cursor)
    backend = PgPqIngestBackend(pool)  # type: ignore[arg-type]

    await backend.upsert_hub_inverter_pq_batch([])

    assert cursor.executed == []
    assert pool._conn.committed is False


async def test_insert_raw_index_executes_and_commits() -> None:
    cursor = FakeCursor()
    pool = FakePool(cursor)
    backend = PgPqIngestBackend(pool)  # type: ignore[arg-type]
    row = PqWaveformRawIndex(
        capture_id="11111111-1111-1111-1111-111111111111",
        hub_id="hub-00000",
        ts=datetime(2026, 9, 26, tzinfo=UTC),
        trigger_reason="API_REQUEST",
        blob_ref="2026-09-26/hub-00000/abc.bin",
        channels=2,
    )

    await backend.insert_raw_index(row)

    assert len(cursor.executed) == 1
    sql, params = cursor.executed[0]
    assert "INSERT INTO og.pq_waveform_raw_index" in sql
    assert params["blob_ref"] == "2026-09-26/hub-00000/abc.bin"
    assert pool._conn.committed is True


async def test_latest_summaries_maps_rows_back_to_models() -> None:
    ts = datetime(2026, 9, 26, tzinfo=UTC)
    from opengrid.pq_ingest.pg_backend import _SUMMARY_COLUMNS

    row_tuple = ("hub-00000", ts) + (None,) * (len(_SUMMARY_COLUMNS) - 2)
    cursor = FakeCursor(responses=[[row_tuple]])
    pool = FakePool(cursor)
    backend = PgPqIngestBackend(pool)  # type: ignore[arg-type]

    rows = await backend.latest_summaries(["hub-00000"], since=ts)

    assert len(rows) == 1
    assert rows[0].hub_id == "hub-00000"
    assert rows[0].ts == ts
