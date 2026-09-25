"""Pure-logic pieces of `db.py` that don't need a live Postgres (BUILD.md S5: no DB in local tests)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from opengrid.selector import db


def test_interval_key_uses_isoformat_for_datetimes():
    ts = datetime(2026, 9, 26, 12, 15, tzinfo=UTC)
    assert db._interval_key(ts) == ts.isoformat()


def test_interval_key_falls_back_to_str_for_non_datetime():
    assert db._interval_key("2026-09-26T12:15:00") == "2026-09-26T12:15:00"


async def test_load_frozen_commitments_shapes_rows_by_obligation_and_interval(monkeypatch):
    obligation_id = uuid4()
    t0 = datetime(2026, 9, 26, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 9, 26, 0, 15, tzinfo=UTC)

    async def _fake_rows(horizon_start, horizon_end):
        return [(obligation_id, t0, 5.0), (obligation_id, t1, 5.0)]

    monkeypatch.setattr(db, "load_frozen_commitments_rows", _fake_rows)

    frozen = await db.load_frozen_commitments("2026-09-26T00:00:00", "2026-09-27T00:00:00")

    assert frozen == {obligation_id: {t0.isoformat(): 5.0, t1.isoformat(): 5.0}}
