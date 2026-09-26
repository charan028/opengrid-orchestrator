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


def test_market_columns_are_read_without_requiring_migration_0025():
    """The contract's market/utility come through `to_jsonb(c) ->> ...`, so the selector's queries keep
    working on a database where MARKET-MODEL's 0025 columns do not exist yet (NULL -> FREE)."""
    for sql in (db._OFFERED_OPPORTUNITIES_SQL, db._OBLIGATION_TERMS_SQL):
        assert "to_jsonb(c) ->> 'market'" in sql
        assert "to_jsonb(c) ->> 'utility_id'" in sql
        assert "c.market" not in sql


def test_ercot_solar_share_uses_core_ids_and_the_core_ratio():
    """The plan's ERCOT source reads core's feed ids and computes the share with core's one formula:
    clamped to [0, 1], no share of a non-positive load, missing values skipped."""
    import inspect

    from opengrid.core import solar_share as core

    assert "ERCOT_SOLAR_PRODUCT = " not in inspect.getsource(db)  # the ids live in core only
    assert db.ERCOT_SOLAR_PRODUCT is core.ERCOT_SOLAR_PRODUCT
    assert "%(load_product)s" in db._ERCOT_SOLAR_SHARE_SQL and core.ERCOT_SOLAR_PRODUCT == "np4-737-cd"
    shares = db.ercot_shares_from_rows(
        [(12, 9_000.0, 60_000.0), (13, -5.0, 50_000.0), (14, 1.0, 0.0), (15, None, 1.0)]
    )
    assert shares == {12: 0.15, 13: 0.0}


def test_hold_floor_query_and_retention_prune_shape():
    assert "hold_floor_kwh[" in db._HOLD_FLOORS_SQL and "1 + floor(" in db._HOLD_FLOORS_SQL
    assert "ORDER BY bank_id, created_at DESC" in db._HOLD_FLOORS_SQL
    assert "DELETE FROM og.plan_energy_value" in db._PRUNE_ENERGY_VALUE_SQL
    assert "created_at < now() - make_interval(days => %(days)s)" in db._PRUNE_ENERGY_VALUE_SQL
    assert db.ENERGY_VALUE_RETENTION_DAYS == 7


def test_energy_value_threshold_query_indexes_the_array_at_the_requested_time():
    sql = db._ENERGY_VALUE_THRESHOLDS_SQL
    assert "discharge_threshold_usd_per_mwh[" in sql and "1 + floor(" in sql  # Postgres arrays are 1-based
    assert "horizon_start <= %(at)s AND horizon_end > %(at)s" in sql
    assert "ORDER BY bank_id, created_at DESC" in sql  # newest plan wins


async def test_insert_plan_analytics_writes_the_three_tables_in_one_transaction(monkeypatch):
    executed: list[tuple[str, object]] = []

    class _Cursor:
        async def execute(self, sql, params=None):
            executed.append((sql, params))

        async def executemany(self, sql, rows):
            executed.append((sql, list(rows)))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Tx:
        async def __aenter__(self):
            executed.append(("BEGIN", None))

        async def __aexit__(self, *exc):
            executed.append(("COMMIT", None))
            return False

    class _Conn:
        def cursor(self):
            return _Cursor()

        def transaction(self):
            return _Tx()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Pool:
        def connection(self):
            return _Conn()

    async def _pool():
        return _Pool()

    monkeypatch.setattr(db, "get_pool", _pool)

    await db.insert_plan_analytics({"plan_id": 1}, [{"row": 1}], [{"row": 2}])

    assert executed[0] == ("BEGIN", None) and executed[-1] == ("COMMIT", None)
    tables = [sql for sql, _ in executed[1:-1]]
    assert "og.plan_value" in tables[0]
    assert "og.plan_shadow_obligation" in tables[1]
    assert "og.plan_energy_value" in tables[2]


async def test_load_unfit_price_series_returns_the_flagged_zones(monkeypatch):
    seen: dict[str, object] = {}

    class _Cursor:
        async def execute(self, sql, params=None):
            seen["sql"], seen["params"] = sql, params

        def __aiter__(self):
            return self._rows()

        async def _rows(self):
            for row in (("LZ_WEST",), ("LZ_AEN",)):
                yield row

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Conn:
        def cursor(self):
            return _Cursor()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Pool:
        def connection(self):
            return _Conn()

    async def _pool():
        return _Pool()

    monkeypatch.setattr(db, "get_pool", _pool)
    start = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)

    unfit = await db.load_unfit_price_series(start, start + timedelta(hours=24))

    assert unfit == frozenset({"LZ_WEST", "LZ_AEN"})
    assert "firm_fitness = 'NOT_FOR_FIRM'" in str(seen["sql"]) and "kind = 'price'" in str(seen["sql"])


async def test_load_frozen_commitments_shapes_rows_by_obligation_and_interval(monkeypatch):
    obligation_id = uuid4()
    t0 = datetime(2026, 9, 26, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 9, 26, 0, 15, tzinfo=UTC)

    async def _fake_rows(horizon_start, horizon_end):
        return [(obligation_id, t0, 5.0), (obligation_id, t1, 5.0)]

    monkeypatch.setattr(db, "load_frozen_commitments_rows", _fake_rows)

    frozen = await db.load_frozen_commitments("2026-09-26T00:00:00", "2026-09-27T00:00:00")

    assert frozen == {obligation_id: {t0.isoformat(): 5.0, t1.isoformat(): 5.0}}
