"""Unit tests for `opengrid.invariants.queries`' persistence and summary reads, against a minimal
fake pool/cursor (no real Postgres, BUILD.md S5) -- mirrors `tests/unit/health/test_queries.py`'s
pattern."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from opengrid.core.nameplate import NAMEPLATE_HUB_EXISTS_SQL
from opengrid.invariants import queries
from opengrid.invariants.models import CheckState, Violation

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)


class FakeCursor:
    def __init__(
        self,
        fetchall_result: list | None = None,
        fetchone_result: tuple | None = None,
        fetchone_results: list | None = None,
    ) -> None:
        self.executed: list[tuple[str, dict | list | None]] = []
        self._fetchall_result = fetchall_result or []
        self._fetchone_result = fetchone_result
        # For call sequences where each execute()/fetchone() pair needs a different answer (e.g.
        # insert_violations' per-row ON CONFLICT loop): consumed in order, falling back to
        # `_fetchone_result` once exhausted.
        self._fetchone_results = list(fetchone_results) if fetchone_results is not None else None

    async def execute(self, sql, params=None):
        self.executed.append((str(sql), params))

    async def executemany(self, sql, params_seq):
        self.executed.append((str(sql), list(params_seq)))

    async def fetchall(self):
        return self._fetchall_result

    async def fetchone(self):
        if self._fetchone_results is not None:
            return self._fetchone_results.pop(0) if self._fetchone_results else None
        return self._fetchone_result

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


async def test_get_check_state_returns_empty_when_never_run() -> None:
    cursor = FakeCursor(fetchone_result=None)
    pool = FakePool(cursor)

    state = await queries.get_check_state(pool, "K1_RESERVE_BREACH")

    assert state == CheckState.empty("K1_RESERVE_BREACH")


async def test_get_check_state_parses_existing_row() -> None:
    cursor = FakeCursor(
        fetchone_result=("K1_RESERVE_BREACH", NOW, 120, 2, 7, {"since": "2026-09-26T00:00:00+00:00"})
    )
    pool = FakePool(cursor)

    state = await queries.get_check_state(pool, "K1_RESERVE_BREACH")

    assert state.last_run_at == NOW
    assert state.last_run_ms == 120
    assert state.last_violations == 2
    assert state.total_violations == 7
    assert state.watermark == {"since": "2026-09-26T00:00:00+00:00"}


async def test_upsert_check_state_commits() -> None:
    cursor = FakeCursor()
    pool = FakePool(cursor)

    await queries.upsert_check_state(
        pool,
        "K1_RESERVE_BREACH",
        ran_at=NOW,
        run_ms=50,
        violation_count=1,
        total_violations=3,
        watermark={"since": NOW.isoformat()},
    )

    assert pool._conn.committed is True
    assert len(cursor.executed) == 1
    _sql, params = cursor.executed[0]
    assert params["check_name"] == "K1_RESERVE_BREACH"
    assert params["last_violations"] == 1
    assert params["total_violations"] == 3


async def test_insert_violations_noop_on_empty_list() -> None:
    cursor = FakeCursor()
    pool = FakePool(cursor)

    await queries.insert_violations(pool, "K1_RESERVE_BREACH", [])

    assert cursor.executed == []
    assert pool._conn.committed is False


async def test_insert_violations_writes_one_row_per_violation() -> None:
    # Both rows are genuinely new (RETURNING gives back a row each time -- no ON CONFLICT hit).
    cursor = FakeCursor(fetchone_results=[("hub-1|t1",), ("hub-2|t1",)])
    pool = FakePool(cursor)
    violations = [
        Violation(scope={"hub_id": "hub-1"}, dedupe_key="hub-1|t1", detail={"soc_kwh": 1.0}),
        Violation(scope={"hub_id": "hub-2"}, dedupe_key="hub-2|t1", detail={"soc_kwh": 1.5}),
    ]

    newly_inserted = await queries.insert_violations(pool, "K1_RESERVE_BREACH", violations)

    assert pool._conn.committed is True
    assert len(cursor.executed) == 2  # one INSERT ... ON CONFLICT per violation
    _sql, params = cursor.executed[0]
    assert params["check_name"] == "K1_RESERVE_BREACH"
    assert params["dedupe_key"] == "hub-1|t1"
    assert newly_inserted == violations


async def test_insert_violations_skips_already_seen_dedupe_keys() -> None:
    """The idempotency fix: a violation whose dedupe_key already exists (ON CONFLICT DO NOTHING, no
    RETURNING row) is not counted as newly inserted -- callers must not re-count it into a running total
    or a Prometheus counter."""
    cursor = FakeCursor(fetchone_results=[("hub-1|t1",), None])  # second row hits ON CONFLICT
    pool = FakePool(cursor)
    violations = [
        Violation(scope={"hub_id": "hub-1"}, dedupe_key="hub-1|t1", detail={}),
        Violation(scope={"hub_id": "hub-2"}, dedupe_key="hub-2|t1", detail={}),
    ]

    newly_inserted = await queries.insert_violations(pool, "K1_RESERVE_BREACH", violations)

    assert [v.dedupe_key for v in newly_inserted] == ["hub-1|t1"]


async def test_read_summary_maps_totals_and_last_counts() -> None:
    rows = [
        ("K1_RESERVE_BREACH", NOW, 3, 1),
        ("K2_DOUBLE_SOLD", NOW, 12.5, 1),
        ("K13_LOCK_VIOLATION", NOW, 0, 0),
        ("K13_OUTAGE_GAP", NOW, 2, 1),
        ("ORPHAN_RESERVATION", NOW, 9, 2),  # total=9 (ever seen), last_violations=2 (current)
        ("ORPHAN_COMMITMENT", NOW, 4, 0),
    ]
    cursor = FakeCursor(fetchall_result=rows)
    pool = FakePool(cursor)

    summary = await queries.read_summary(pool)

    assert summary.reserve_breaches == 3
    assert summary.double_sold_kwh == 12.5
    assert summary.lock_violations == 0
    assert summary.outage_gaps == 2
    assert summary.orphan_reservations == 2  # last run's count, not the cumulative total
    assert summary.orphan_commitments == 0
    assert summary.as_of == NOW


async def test_fetch_reserve_breach_candidates_uses_compound_cursor_and_upper_bound() -> None:
    cursor = FakeCursor(fetchall_result=[("hub-2", NOW, 1.0, -5.0, 2.0)])
    pool = FakePool(cursor)

    rows, new_cursor = await queries.fetch_reserve_breach_candidates(
        pool, since_ts=NOW, since_hub_id="hub-1", upper_bound=NOW, limit=100
    )

    assert rows == [("hub-2", NOW, 1.0, -5.0, 2.0)]
    assert new_cursor == (NOW, "hub-2")
    _sql, params = cursor.executed[0]
    assert params["since_ts"] == NOW
    assert params["since_hub_id"] == "hub-1"
    assert params["upper_bound"] == NOW


async def test_fetch_reserve_breach_candidates_no_cursor_on_empty_result() -> None:
    cursor = FakeCursor(fetchall_result=[])
    pool = FakePool(cursor)

    rows, new_cursor = await queries.fetch_reserve_breach_candidates(
        pool, since_ts=NOW, since_hub_id="", upper_bound=NOW
    )

    assert rows == []
    assert new_cursor is None


async def test_get_trace_watermark_defaults_to_zero_for_unknown_stream() -> None:
    cursor = FakeCursor(fetchone_result=None)
    pool = FakePool(cursor)

    assert await queries.get_trace_watermark(pool, "unknown-stream") == 0


async def test_get_trace_watermark_returns_stored_value() -> None:
    cursor = FakeCursor(fetchone_result=(42,))
    pool = FakePool(cursor)

    assert await queries.get_trace_watermark(pool, "guardian_verdict") == 42


async def test_fetch_shortfall_events_parses_payload() -> None:
    cursor = FakeCursor(fetchall_result=[(NOW, 40.0, "cyc-1"), (NOW, None, None)])
    pool = FakePool(cursor)

    events = await queries.fetch_shortfall_events(
        pool, obligation_id="ob-1", window_start=NOW, window_end=NOW
    )

    assert events[0].shortfall_kw == 40.0
    assert events[0].cycle_id == "cyc-1"
    assert events[1].shortfall_kw == 0.0  # a NULL payload field defaults to 0.0 (no shortfall reported)


async def test_fetch_measured_need_sample_returns_none_without_a_service_profile() -> None:
    cursor = FakeCursor(fetchone_result=None)
    pool = FakePool(cursor)

    sample = await queries.fetch_measured_need_sample(pool, obligation_id="ob-1", at=NOW)

    assert sample is None


async def test_fetch_measured_need_sample_site_meter() -> None:
    pytest.importorskip("opengrid.site_ingest")  # ships with the customer-services package
    cursor = FakeCursor(
        fetchone_results=[
            ("cust-1", "site_meter:site-dc-01:p_kw"),  # profile lookup
            (42.0, "GOOD"),  # reading lookup
        ]
    )
    pool = FakePool(cursor)

    sample = await queries.fetch_measured_need_sample(pool, obligation_id="ob-1", at=NOW)

    assert sample is not None
    assert sample.kind == "site_meter"
    assert sample.field == "p_kw"
    assert sample.value == 42.0
    assert sample.limit is None
    assert sample.quality == "GOOD"


async def test_fetch_measured_need_sample_corridor() -> None:
    pytest.importorskip("opengrid.site_ingest")  # ships with the customer-services package
    cursor = FakeCursor(
        fetchone_results=[
            ("cust-1", "corridor:corr-07:i_ac_a"),
            (85.0, 100.0, "GOOD"),
        ]
    )
    pool = FakePool(cursor)

    sample = await queries.fetch_measured_need_sample(pool, obligation_id="ob-1", at=NOW)

    assert sample is not None
    assert sample.kind == "corridor"
    assert sample.value == 85.0
    assert sample.limit == 100.0


async def test_fetch_measured_need_sample_none_for_unparseable_ref() -> None:
    cursor = FakeCursor(fetchone_results=[("cust-1", "not-a-valid-ref")])
    pool = FakePool(cursor)

    sample = await queries.fetch_measured_need_sample(pool, obligation_id="ob-1", at=NOW)

    assert sample is None


async def test_fetch_measured_need_sample_none_when_no_reading_yet() -> None:
    cursor = FakeCursor(fetchone_results=[("cust-1", "site_meter:site-dc-01:p_kw"), None])
    pool = FakePool(cursor)

    sample = await queries.fetch_measured_need_sample(pool, obligation_id="ob-1", at=NOW)

    assert sample is None


async def test_upsert_trace_watermark_commits() -> None:
    cursor = FakeCursor()
    pool = FakePool(cursor)

    await queries.upsert_trace_watermark(pool, "guardian_verdict", next_from_seq=7)

    assert pool._conn.committed is True
    _sql, params = cursor.executed[0]
    assert params == {"stream_id": "guardian_verdict", "next_from_seq": 7}


_CAP_ROW = ("bank-000", 600.0, 0.0, 78.4, 15.68, 20.0, 0.9487, 0.9487, 50.0, "online")


async def test_fetch_bank_capability_inputs_selects_hub_units_when_column_exists() -> None:
    """K2 units (migration 0032): `h.units` is selected and returned as the row's 11th element."""
    cursor = FakeCursor(fetchone_result=(True,), fetchall_result=[(*_CAP_ROW, 2, True)])
    pool = FakePool(cursor)

    rows = await queries.fetch_bank_capability_inputs(pool)

    assert rows == [(*_CAP_ROW, 2, True)]
    assert "information_schema.columns" in cursor.executed[0][0]
    assert "h.units" in cursor.executed[1][0]
    # utility_scale: the one nameplate rule the guardian's G-02 reads too (core.nameplate: SUBSTATION, MOBILE_STORAGE)
    assert NAMEPLATE_HUB_EXISTS_SQL in cursor.executed[1][0]


async def test_fetch_bank_capability_inputs_units_none_when_column_missing() -> None:
    """A pre-0032 database: no `og.hub.units` column -- the select must not reference it, and every row's
    `units` is None (the capability cap then fails closed to one unit)."""
    cursor = FakeCursor(fetchone_result=(False,), fetchall_result=[(*_CAP_ROW, None, False)])
    pool = FakePool(cursor)

    rows = await queries.fetch_bank_capability_inputs(pool)

    assert rows == [(*_CAP_ROW, None, False)]
    assert "h.units" not in cursor.executed[1][0]
    assert "NULL::smallint" in cursor.executed[1][0]


async def test_read_summary_all_zero_and_as_of_none_when_never_run() -> None:
    cursor = FakeCursor(fetchall_result=[])
    pool = FakePool(cursor)

    summary = await queries.read_summary(pool)

    assert summary.reserve_breaches == 0
    assert summary.double_sold_kwh == 0.0
    assert summary.as_of is None


def test_every_k13_exception_the_engine_traces_is_accepted_by_the_lock_check() -> None:
    """R6: the engine traces R-OPERATOR-OVERRIDE shortfalls (G-19 signs them); the invariants lock check must
    accept them -- one shared list (`core.reasons.K13_SHORTFALL_REASONS`), never a copy."""
    from opengrid.core.reasons import K13_SHORTFALL_REASONS, R_AS_RELEASE, R_OPERATOR_OVERRIDE, R_SUBSTITUTION
    from opengrid.engine.gateways import _K13_SHORTFALL_REASONS
    from opengrid.invariants.queries import ALLOWED_K13_TRACE_REASONS

    assert R_OPERATOR_OVERRIDE in ALLOWED_K13_TRACE_REASONS
    assert _K13_SHORTFALL_REASONS is K13_SHORTFALL_REASONS
    assert K13_SHORTFALL_REASONS | {R_AS_RELEASE, R_SUBSTITUTION} == ALLOWED_K13_TRACE_REASONS
