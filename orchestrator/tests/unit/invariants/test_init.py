"""Unit tests for `opengrid.invariants`'s orchestration (`run_once`/`run_due`/`run_trace_verify_once`)
with `invariants.queries` monkeypatched -- no real Postgres, mirroring
`tests/unit/health/test_init.py`'s pattern."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import opengrid.invariants as invariants
from opengrid.invariants.models import (
    CHECK_K1_RESERVE_BREACH,
    CHECK_K2_DOUBLE_SOLD,
    CHECK_K13_LOCK_VIOLATION,
    CHECK_ORPHAN_COMMITMENT,
    CHECK_ORPHAN_RESERVATION,
    CheckState,
)
from opengrid.platform.config import Config

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)


class _FakeQueries:
    def __init__(self) -> None:
        self.reserve_rows: list[tuple] = []
        self.reservation_agg_rows: list[tuple] = []
        self.lock_commitment_candidates: list[tuple] = []
        self.grant_cycle_series_by_obligation: dict = {}
        self.covering_trace_at_by_obligation: dict = {}
        self.orphan_reservation_rows: list[tuple] = []
        self.orphan_commitment_rows: list[tuple] = []
        self.states: dict[str, CheckState] = {}
        self.upserts: list[dict] = []
        self.inserted_violations: dict[str, list] = {}

    async def get_check_state(self, pool, check_name):
        return self.states.get(check_name, CheckState.empty(check_name))

    async def fetch_reserve_breach_candidates(self, pool, *, since, limit=5000):
        return self.reserve_rows, (self.reserve_rows[-1][1] if self.reserve_rows else None)

    async def fetch_reservation_aggregates(self, pool, *, horizon_start):
        return self.reservation_agg_rows

    async def fetch_lock_commitment_candidates(self, pool, *, since, now, limit=5000):
        return (
            self.lock_commitment_candidates,
            (self.lock_commitment_candidates[-1][2] if self.lock_commitment_candidates else None),
        )

    async def fetch_grant_cycle_series(self, pool, *, obligation_id, window_start, window_end):
        return self.grant_cycle_series_by_obligation.get(obligation_id, [])

    async def fetch_earliest_covering_trace_at(self, pool, *, obligation_id, window_start, window_end):
        return self.covering_trace_at_by_obligation.get(obligation_id)

    async def fetch_orphan_reservations(self, pool, *, limit=5000):
        return self.orphan_reservation_rows

    async def fetch_orphan_commitments(self, pool, *, limit=5000):
        return self.orphan_commitment_rows

    async def insert_violations(self, pool, check_name, violations):
        self.inserted_violations.setdefault(check_name, []).extend(violations)

    async def upsert_check_state(
        self, pool, check_name, *, ran_at, run_ms, violation_count, total_violations, watermark
    ):
        self.upserts.append(
            {
                "check_name": check_name,
                "violation_count": violation_count,
                "total_violations": total_violations,
                "watermark": watermark,
            }
        )
        self.states[check_name] = CheckState(
            check_name=check_name,
            last_run_at=ran_at,
            last_run_ms=run_ms,
            last_violations=violation_count,
            total_violations=total_violations,
            watermark=watermark,
        )


@pytest.fixture(autouse=True)
def _reset_invariants_module(monkeypatch: pytest.MonkeyPatch) -> None:
    invariants.configure(pool=object(), cfg=Config({}))  # type: ignore[arg-type]
    monkeypatch.setattr(invariants, "_now", lambda: NOW)
    yield
    invariants._pool = None


@pytest.fixture
def fake_queries(monkeypatch: pytest.MonkeyPatch) -> _FakeQueries:
    fake = _FakeQueries()
    monkeypatch.setattr(invariants, "queries", fake)
    return fake


async def test_run_once_reports_zero_on_clean_data(fake_queries: _FakeQueries) -> None:
    outcomes = await invariants.run_once()

    assert outcomes[CHECK_K1_RESERVE_BREACH].count == 0
    assert outcomes[CHECK_K2_DOUBLE_SOLD].count == 0
    assert outcomes[CHECK_K13_LOCK_VIOLATION].count == 0
    assert outcomes[CHECK_ORPHAN_RESERVATION].count == 0
    assert outcomes[CHECK_ORPHAN_COMMITMENT].count == 0
    assert len(fake_queries.upserts) == 5  # every check persisted its (empty) result


async def test_run_once_detects_seeded_reserve_breach(fake_queries: _FakeQueries) -> None:
    fake_queries.reserve_rows = [("hub-1", NOW, 1.0, -5.0, 2.0)]

    outcomes = await invariants.run_once()

    assert outcomes[CHECK_K1_RESERVE_BREACH].count == 1


async def test_run_once_detects_seeded_double_sold(fake_queries: _FakeQueries) -> None:
    from datetime import timedelta

    fake_queries.reservation_agg_rows = [("bank-000", NOW, NOW + timedelta(minutes=15), 700.0, 600.0)]

    outcomes = await invariants.run_once()

    assert outcomes[CHECK_K2_DOUBLE_SOLD].count == 1
    assert outcomes[CHECK_K2_DOUBLE_SOLD].total_magnitude == pytest.approx(25.0)


async def test_run_once_detects_seeded_lock_violation(fake_queries: _FakeQueries) -> None:
    from datetime import timedelta

    fake_queries.lock_commitment_candidates = [("ob-1", NOW, NOW + timedelta(minutes=15), 100.0)]
    fake_queries.grant_cycle_series_by_obligation["ob-1"] = [(NOW + timedelta(seconds=2), 10.0)]
    # No covering trace registered for "ob-1" -- fetch_earliest_covering_trace_at defaults to None.

    outcomes = await invariants.run_once()

    assert outcomes[CHECK_K13_LOCK_VIOLATION].count == 1


async def test_run_once_clean_when_lock_dip_is_covered(fake_queries: _FakeQueries) -> None:
    from datetime import timedelta

    interval_start = NOW
    fake_queries.lock_commitment_candidates = [("ob-1", interval_start, NOW + timedelta(minutes=15), 100.0)]
    dip_at = interval_start + timedelta(seconds=2)
    fake_queries.grant_cycle_series_by_obligation["ob-1"] = [(dip_at, 10.0)]
    fake_queries.covering_trace_at_by_obligation["ob-1"] = dip_at  # covered at or before the dip

    outcomes = await invariants.run_once()

    assert outcomes[CHECK_K13_LOCK_VIOLATION].count == 0


async def test_run_once_detects_seeded_orphans(fake_queries: _FakeQueries) -> None:
    fake_queries.orphan_reservation_rows = [("res-1", "ob-1", "bank-000", NOW)]
    fake_queries.orphan_commitment_rows = [("com-1", "ob-2", NOW, "REJECTED")]

    outcomes = await invariants.run_once()

    assert outcomes[CHECK_ORPHAN_RESERVATION].count == 1
    assert outcomes[CHECK_ORPHAN_COMMITMENT].count == 1


async def test_run_once_carries_forward_watermark_across_runs(fake_queries: _FakeQueries) -> None:
    fake_queries.reserve_rows = [("hub-1", NOW, 1.0, -5.0, 2.0)]
    await invariants.run_once()
    first_watermark = fake_queries.upserts[0]["watermark"]
    assert first_watermark == {"since": NOW.isoformat()}

    # Second run: no new rows -- total_violations must not double-count the same violation.
    fake_queries.reserve_rows = []
    await invariants.run_once()
    second_upsert = next(u for u in fake_queries.upserts[5:] if u["check_name"] == CHECK_K1_RESERVE_BREACH)
    assert second_upsert["violation_count"] == 0
    assert second_upsert["total_violations"] == 1  # unchanged from the first run's total


async def test_run_due_is_a_noop_when_not_configured() -> None:
    invariants._pool = None
    invariants._cadence = None
    invariants._trace_cadence = None
    await invariants.run_due()  # must not raise


async def test_run_due_swallows_check_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(invariants, "run_once", _boom)
    monkeypatch.setattr(invariants, "run_trace_verify_once", _boom)

    await invariants.run_due()  # must not raise, even though both checks are due and both fail
