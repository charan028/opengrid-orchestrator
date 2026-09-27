"""r3.4.3: K13 false positives on undeployed capacity holds (DISPATCH's diagnosis, lead's fix list).

(1) A hold (ERCOT_AS / REGULATED_CAPACITY) has a K13 floor of 0 kW outside every og.as_deployment window and
the deployed kW inside one; an undeployed hold's 0 kW R-GRANT-AS-HOLD cycles are the commitment kept.
(2) The current commitment is the one nothing supersedes (not `supersedes IS NULL`).
(3) On a tie between a real cycle sample and an implicit gap, the sample is the dip.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from opengrid import invariants
from opengrid.core.reasons import K13_SHORTFALL_REASONS, R_COMMIT_LOCK_INFEASIBLE
from opengrid.invariants import checks, queries
from opengrid.invariants.models import CHECK_K13_LOCK_VIOLATION, CHECK_K13_OUTAGE_GAP
from opengrid.platform.config import Config

from .test_init import NOW, _FakeQueries

T0 = datetime(2026, 9, 27, 6, 0, tzinfo=UTC)
ECRS_KW = 5_000.0


def _series(start: datetime, kws: list[float], step_s: float = 2.0) -> list[tuple[datetime, float, str]]:
    return [(start + timedelta(seconds=step_s * i), kw, f"cyc-{i}") for i, kw in enumerate(kws)]


# --- (1) the hold floor ------------------------------------------------------------------------------------------


def test_hold_floor_is_zero_outside_deployments_and_the_deployed_kw_inside() -> None:
    w = checks.HoldWindow(T0 + timedelta(minutes=5), T0 + timedelta(minutes=10), 3_000.0)
    assert checks.hold_floor_kw(T0, (w,)) == 0.0
    assert checks.hold_floor_kw(T0 + timedelta(minutes=5), (w,)) == 3_000.0
    assert checks.hold_floor_kw(T0 + timedelta(minutes=10), (w,)) == 0.0  # end is exclusive
    assert checks.hold_floor_kw(T0, ()) == 0.0


def test_an_undeployed_ecrs_hold_is_never_a_dip() -> None:
    """Granted 0 kW every cycle (R-GRANT-AS-HOLD) with long silences, no deployment: no dip at all."""
    end = T0 + timedelta(minutes=15)
    cycles = _series(T0, [0.0] * 5) + _series(T0 + timedelta(minutes=10), [0.0] * 3)
    assert (
        checks.find_dip(
            interval_start=T0, interval_end=end, committed_kw=ECRS_KW, cycles=cycles, hold_windows=()
        )
        is None
    )
    # the same series on a non-hold obligation is a dip (unchanged behaviour)
    assert (
        checks.find_dip(interval_start=T0, interval_end=end, committed_kw=ECRS_KW, cycles=cycles) is not None
    )


def test_a_deployed_hold_under_delivering_is_a_dip_at_the_deployed_floor() -> None:
    start = T0 + timedelta(seconds=10)
    window = checks.HoldWindow(start, start + timedelta(seconds=10), 4_000.0)  # requested 4 MW of 5 MW
    cycles = _series(T0, [0.0] * 5 + [2_500.0] * 5 + [0.0] * 5)
    dip = checks.find_dip(
        interval_start=T0,
        interval_end=T0 + timedelta(seconds=30),
        committed_kw=ECRS_KW,
        cycles=cycles,
        hold_windows=(window,),
    )
    assert dip is not None and dip.kw == 2_500.0 and dip.floor_kw == 4_000.0 and not dip.is_gap
    assert dip.cycle_id == "cyc-5"


def test_a_deployed_hold_delivering_its_deployed_kw_is_not_a_dip() -> None:
    start = T0 + timedelta(seconds=10)
    window = checks.HoldWindow(start, start + timedelta(seconds=10), 4_000.0)
    cycles = _series(T0, [0.0] * 5 + [4_000.0] * 5 + [0.0] * 5)
    assert (
        checks.find_dip(
            interval_start=T0,
            interval_end=T0 + timedelta(seconds=30),
            committed_kw=ECRS_KW,
            cycles=cycles,
            hold_windows=(window,),
        )
        is None
    )


def test_a_silence_during_a_deployment_is_an_outage_gap_at_its_start() -> None:
    deploy_at = T0 + timedelta(seconds=100)
    window = checks.HoldWindow(deploy_at, T0 + timedelta(seconds=400), ECRS_KW)
    cycles = _series(T0, [0.0] * 3) + _series(T0 + timedelta(seconds=300), [ECRS_KW] * 3)
    dip = checks.find_dip(
        interval_start=T0,
        interval_end=T0 + timedelta(seconds=306),
        committed_kw=ECRS_KW,
        cycles=cycles,
        hold_windows=(window,),
    )
    assert dip is not None and dip.is_gap and dip.at == deploy_at and dip.floor_kw == ECRS_KW


# --- (3) tie: the real sample wins over the gap ------------------------------------------------------------------


def test_on_a_tie_the_real_cycle_sample_is_the_dip_not_the_gap() -> None:
    cycles = [(T0 + timedelta(seconds=60), 0.0, "cyc-zero")]  # a 60 s leading gap AND a 0 kW sample
    dip = checks.find_dip(
        interval_start=T0, interval_end=T0 + timedelta(seconds=62), committed_kw=100.0, cycles=cycles
    )
    assert dip is not None and not dip.is_gap and dip.cycle_id == "cyc-zero"
    # ...so a trace covering that cycle (a K13 exception from the one shared list) covers the dip
    assert R_COMMIT_LOCK_INFEASIBLE in K13_SHORTFALL_REASONS
    rows = [("ob", T0, T0, dip.floor_kw, dip, None, frozenset({"cyc-zero"}), [], False, False)]
    assert checks.classify_lock_rows(rows) == ([], [])


# --- (2) current commitments: NOT EXISTS a superseding row -----------------------------------------------------


class _Cursor:
    def __init__(self) -> None:
        self.sql = ""

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, sql: str, params: object = None) -> None:
        self.sql = sql

    async def fetchall(self) -> list[object]:
        return []


class _Conn:
    def __init__(self, cur: _Cursor) -> None:
        self.cur = cur

    async def __aenter__(self) -> _Conn:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return self.cur


class _Pool:
    def __init__(self) -> None:
        self.cur = _Cursor()

    def connection(self) -> _Conn:
        return _Conn(self.cur)


async def test_lock_candidates_are_the_rows_nothing_supersedes() -> None:
    pool = _Pool()
    await queries.fetch_lock_commitment_candidates(pool, since=T0, now=T0)  # type: ignore[arg-type]
    sql = " ".join(pool.cur.sql.split())
    assert "NOT EXISTS (SELECT 1 FROM og.commitment c2 WHERE c2.supersedes = c.commitment_id)" in sql
    assert "c.supersedes IS NULL" not in sql


# --- end to end through the K13 run (fake queries) ---------------------------------------------------------------


@pytest.fixture
def fake_queries(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeQueries]:
    invariants.configure(pool=object(), cfg=Config({}))  # type: ignore[arg-type]
    monkeypatch.setattr(invariants, "_now", lambda: NOW + timedelta(hours=1))
    fake = _FakeQueries()
    fake.hold_windows_by_obligation = {}  # type: ignore[attr-defined]
    monkeypatch.setattr(invariants, "queries", fake)
    yield fake
    invariants._pool = None


async def test_run_once_an_undeployed_ecrs_hold_produces_zero_k13_violations(
    fake_queries: _FakeQueries,
) -> None:
    start, end = NOW, NOW + timedelta(minutes=15)
    fake_queries.lock_commitment_candidates = [("ob-ecrs", start, end, ECRS_KW)]
    fake_queries.grant_cycle_series_by_obligation["ob-ecrs"] = _series(start, [0.0] * 10)  # then silence
    fake_queries.hold_windows_by_obligation["ob-ecrs"] = ()  # type: ignore[attr-defined]

    outcomes = await invariants.run_once()

    assert outcomes[CHECK_K13_LOCK_VIOLATION].count == 0
    assert outcomes[CHECK_K13_OUTAGE_GAP].count == 0


async def test_run_once_a_deployed_hold_under_delivering_produces_one_violation(
    fake_queries: _FakeQueries,
) -> None:
    start, end = NOW, NOW + timedelta(seconds=20)
    fake_queries.lock_commitment_candidates = [("ob-ecrs", start, end, ECRS_KW)]
    fake_queries.grant_cycle_series_by_obligation["ob-ecrs"] = _series(start, [0.0] * 3 + [3_000.0] * 7)
    fake_queries.hold_windows_by_obligation["ob-ecrs"] = (  # type: ignore[attr-defined]
        checks.HoldWindow(start + timedelta(seconds=6), end, ECRS_KW),
    )

    outcomes = await invariants.run_once()

    assert outcomes[CHECK_K13_LOCK_VIOLATION].count == 1
    assert outcomes[CHECK_K13_OUTAGE_GAP].count == 0
    violation = fake_queries.inserted_violations[CHECK_K13_LOCK_VIOLATION][0]
    assert violation.detail["committed_kw"] == ECRS_KW and violation.detail["dip_kw"] == 3_000.0
