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

    async def executemany(self, sql, params_seq):
        self.executed.append((str(sql), list(params_seq)))

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
async def test_record_scada_observations_maps_quality_and_commits_once(
    quality_in: str, quality_out: str
) -> None:
    cursor = FakeCursor()
    pool = FakePool(cursor)
    backend = PgFleetBackend(pool)
    signals = [
        ScadaBankSignal(
            bank_id=bank_id,
            signal="APPARENT_POWER_KVA",
            value=42.0,
            unit="kVA",
            quality=quality_in,
            ts=datetime.now(UTC),
        )
        for bank_id in ("bank-000", "bank-001")
    ]

    await backend.record_scada_observations(signals)

    assert cursor.executed[0][0] == "SET LOCAL synchronous_commit TO OFF"  # soft state, see pg_backend
    sql, rows = cursor.executed[1]
    assert "og.feed_obs" in sql
    assert [r["bank_id"] for r in rows] == ["bank-000", "bank-001"]
    assert {r["series"] for r in rows} == {"APPARENT_POWER_KVA"}
    assert {r["quality"] for r in rows} == {quality_out}
    assert pool._conn.committed is True
