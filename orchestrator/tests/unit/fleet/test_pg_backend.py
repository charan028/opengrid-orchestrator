"""opengrid.fleet.pg_backend: PgFleetBackend.record_scada_observation (dispatch-live pass). Minimal
fake pool/cursor, no real DB (BUILD.md S5), mirroring tests/unit/guardian/test_repo.py's pattern."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from opengrid.core.models.mqtt import ScadaBankSignal
from opengrid.fleet.pg_backend import PgFleetBackend


class FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, dict]] = []

    async def execute(self, sql, params=None):
        self.executed.append((str(sql), params))

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


@pytest.mark.parametrize(
    ("quality_in", "quality_out"),
    [
        ("good", "GOOD"),
        ("stale", "STALE"),
        ("missing", "STALE"),
        ("out_of_range", "ESTIMATED"),
        ("comm_fail", "STALE"),
    ],
)
async def test_record_scada_observation_maps_quality_and_commits(quality_in: str, quality_out: str) -> None:
    cursor = FakeCursor()
    pool = FakePool(cursor)
    backend = PgFleetBackend(pool)
    signal = ScadaBankSignal(
        bank_id="bank-000",
        signal="APPARENT_POWER_KVA",
        value=42.0,
        unit="kVA",
        quality=quality_in,
        ts=datetime.now(UTC),
    )

    await backend.record_scada_observation(signal)

    assert len(cursor.executed) == 1
    sql, params = cursor.executed[0]
    assert "og.feed_obs" in sql
    assert params["bank_id"] == "bank-000"
    assert params["series"] == "APPARENT_POWER_KVA"
    assert params["quality"] == quality_out
    assert pool._conn.committed is True
