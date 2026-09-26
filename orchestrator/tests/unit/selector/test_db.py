"""Pure-logic pieces of `db.py` that don't need a live Postgres (BUILD.md S5: no DB in local tests)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

from opengrid.selector import db


def test_interval_key_uses_isoformat_for_datetimes():
    ts = datetime(2026, 9, 26, 12, 15, tzinfo=UTC)
    assert db._interval_key(ts) == ts.isoformat()


def test_interval_key_normalizes_to_utc():
    """Regression (live 2026-09-26): Postgres returns `timestamptz` in the session zone
    (America/Chicago), while `gate.load_committed` indexes intervals by UTC ISO strings -- a
    `-05:00` key never matched, so every committed obligation silently vanished from the model (C24)."""
    chicago = timezone(timedelta(hours=-5))
    ts = datetime(2026, 9, 26, 7, 15, tzinfo=chicago)
    assert db._interval_key(ts) == datetime(2026, 9, 26, 12, 15, tzinfo=UTC).isoformat()


def test_offered_candidates_exclude_obligations_already_decided():
    """Regression (live 2026-09-26): candidates were selected by `opportunity.state` alone, so an
    obligation already SELECTED/COMMITTED/REJECTED was re-offered (and re-reserved) at every gate."""
    assert "ob.state = 'OFFERED'" in db._OFFERED_OPPORTUNITIES_SQL


def test_interval_key_falls_back_to_str_for_non_datetime():
    assert db._interval_key("2026-09-26T12:15:00") == "2026-09-26T12:15:00"


async def test_load_bank_ids_rows_returns_real_format_ids_from_a_fake_pool(monkeypatch):
    """`load_bank_ids_rows` must return exactly what `og.bank` has -- real `bank-000`-style ids, not a
    fabricated `bank-NN` count-based list (qa/merge-notes.md section 11)."""

    class _FakeCursor:
        def __init__(self, rows):
            self._rows = rows

        async def execute(self, sql, params=None):
            assert "og.bank" in sql

        def __aiter__(self):
            return self._aiter_rows()

        async def _aiter_rows(self):
            for row in self._rows:
                yield row

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _FakeConn:
        def __init__(self, rows):
            self._rows = rows

        def cursor(self):
            return _FakeCursor(self._rows)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _FakePool:
        def __init__(self, rows):
            self._rows = rows

        def connection(self):
            return _FakeConn(self._rows)

    fake_pool = _FakePool([("bank-000",), ("bank-001",), ("bank-039",)])

    async def _fake_get_pool():
        return fake_pool

    monkeypatch.setattr(db, "get_pool", _fake_get_pool)

    bank_ids = await db.load_bank_ids_rows()

    assert bank_ids == ["bank-000", "bank-001", "bank-039"]


async def test_load_frozen_commitments_shapes_rows_by_obligation_and_interval(monkeypatch):
    obligation_id = uuid4()
    t0 = datetime(2026, 9, 26, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 9, 26, 0, 15, tzinfo=UTC)

    async def _fake_rows(horizon_start, horizon_end):
        return [(obligation_id, t0, 5.0), (obligation_id, t1, 5.0)]

    monkeypatch.setattr(db, "load_frozen_commitments_rows", _fake_rows)

    frozen = await db.load_frozen_commitments("2026-09-26T00:00:00", "2026-09-27T00:00:00")

    assert frozen == {obligation_id: {t0.isoformat(): 5.0, t1.isoformat(): 5.0}}
