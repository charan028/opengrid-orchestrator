"""Integration tests for `opengrid.contracts.pg_repo.PgContractsRepo` against real Postgres
(`og_t_ctr` on the server, BUILD.md S5). Exercises admission, the obligation lifecycle, and
re-nomination end to end through the real `opengrid.trace.TraceStore` + `PgTraceBackend` hash chain,
not the in-memory fakes the unit tests use.

Run via `powershell -File tools\\remote.ps1 -Ws ctr -Cmd "cd orchestrator && bash tools/check.sh"`, or
directly with `OG_CONFIG`/`OG_DB` set and Postgres reachable. Skipped automatically when no database
is reachable (BUILD.md S5's "local: no DB").
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

import opengrid.contracts as contracts
from opengrid.contracts.pg_repo import PgContractsRepo
from opengrid.core.models.engine import Contract, RenominationPoint
from opengrid.platform.config import load_config
from opengrid.trace import TraceStore
from opengrid.trace.pg_backend import PgTraceBackend

pytestmark = pytest.mark.asyncio

_CONFIG_PATH = os.environ.get(
    "OG_CONFIG", str((__file__.rsplit("orchestrator", 1)[0]) + "orchestrator/config/test.toml")
)


def _dsn() -> str | None:
    try:
        from opengrid.platform.db import build_dsn

        return build_dsn(load_config(_CONFIG_PATH))
    except Exception:
        return None


def _db_reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=2):
            return True
    except Exception:
        return False


_DSN = _dsn()
_SKIP_REASON = "Postgres not reachable locally; run via tools/remote.ps1 -Ws ctr (BUILD.md S5)"
requires_db = pytest.mark.skipif(_DSN is None or not _db_reachable(_DSN), reason=_SKIP_REASON)


@pytest.fixture
async def pool():  # type: ignore[no-untyped-def]
    assert _DSN is not None
    from opengrid.platform.db import migrate_sync

    migrate_sync(_DSN)
    p = AsyncConnectionPool(_DSN, min_size=1, max_size=4, open=False)
    await p.open(wait=True)
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
async def wired(pool: AsyncConnectionPool):  # type: ignore[no-untyped-def]
    repo = PgContractsRepo(pool)
    trace = TraceStore(PgTraceBackend(pool))
    contracts.configure(repo, trace)
    try:
        yield repo, trace
    finally:
        contracts.reset_for_testing()


def _window() -> tuple[datetime, datetime]:
    start = datetime.now(UTC) + timedelta(hours=1)
    return start, start + timedelta(minutes=15)


@requires_db
async def test_admit_persists_a_real_obligation_and_a_real_trace_chain(wired) -> None:  # type: ignore[no-untyped-def]
    repo, trace = wired
    contract = Contract(
        contract_id=uuid4(),
        customer_id=uuid4(),
        service_type="ERCOT_ENERGY",
        tier="T2",
        profile_ref="test-profile@1",
        start_at=datetime.now(UTC) - timedelta(days=1),
    )
    await repo.upsert_contract(contract)
    start, end = _window()

    opportunity = await contracts.admit(contract.contract_id, start, end, Decimal("10"))

    obligation = await repo.get_obligation_by_opportunity(opportunity.opportunity_id)
    assert obligation is not None
    assert obligation.state == "OFFERED"

    verify = await trace.verify("admission")
    assert verify.ok


@requires_db
async def test_full_lifecycle_offered_to_settled_is_traced(wired) -> None:  # type: ignore[no-untyped-def]
    repo, trace = wired
    contract = Contract(
        contract_id=uuid4(),
        customer_id=uuid4(),
        service_type="DIST_DEFERRAL",
        tier="T1",
        profile_ref="test-profile@1",
        start_at=datetime.now(UTC) - timedelta(days=1),
    )
    await repo.upsert_contract(contract)
    start, end = _window()
    opportunity = await contracts.admit(contract.contract_id, start, end, Decimal("4"))
    obligation = await repo.get_obligation_by_opportunity(opportunity.opportunity_id)
    assert obligation is not None

    await contracts.transition_obligation(obligation.obligation_id, "SELECTED", reason_code="R-GATE-SELECT")
    await contracts.transition_obligation(
        obligation.obligation_id, "COMMITTED", reason_code="R-COMMIT-LOCK-ENTER"
    )
    await contracts.transition_obligation(obligation.obligation_id, "DELIVERING", reason_code=None)
    await contracts.transition_obligation(obligation.obligation_id, "FULFILLED", reason_code="R-FULFILLED")
    settled = await contracts.transition_obligation(obligation.obligation_id, "SETTLED", reason_code=None)

    assert settled.state == "SETTLED"
    stream_id = f"obligation-{obligation.obligation_id}"
    verify = await trace.verify(stream_id)
    assert verify.ok


@requires_db
async def test_committed_obligation_survives_a_rejected_illegal_write(wired) -> None:  # type: ignore[no-untyped-def]
    """K13, against real Postgres: a caller attempting to bypass the lock by writing REJECTED
    straight from COMMITTED must fail, leaving the persisted row untouched."""
    from opengrid.contracts.errors import IllegalTransitionError

    repo, _trace = wired
    contract = Contract(
        contract_id=uuid4(),
        customer_id=uuid4(),
        service_type="PARTNER_CAPACITY",
        tier="T3",
        profile_ref="test-profile@1",
        start_at=datetime.now(UTC) - timedelta(days=1),
    )
    await repo.upsert_contract(contract)
    start, end = _window()
    opportunity = await contracts.admit(contract.contract_id, start, end, Decimal("50"))
    obligation = await repo.get_obligation_by_opportunity(opportunity.opportunity_id)
    assert obligation is not None
    await contracts.transition_obligation(obligation.obligation_id, "SELECTED", reason_code="R-GATE-SELECT")
    committed = await contracts.transition_obligation(
        obligation.obligation_id, "COMMITTED", reason_code="R-COMMIT-LOCK-ENTER"
    )

    with pytest.raises(IllegalTransitionError):
        await contracts.transition_obligation(
            obligation.obligation_id, "REJECTED", reason_code="R-ADMIT-REJECT"
        )

    reread = await repo.get_obligation(obligation.obligation_id)
    assert reread is not None
    assert reread.state == "COMMITTED"
    assert reread.version == committed.version


@requires_db
async def test_renomination_reselects_only_its_own_obligation(wired) -> None:  # type: ignore[no-untyped-def]
    repo, _trace = wired
    contract = Contract(
        contract_id=uuid4(),
        customer_id=uuid4(),
        service_type="PARTNER_CAPACITY",
        tier="T3",
        profile_ref="test-profile@1",
        start_at=datetime.now(UTC) - timedelta(days=2),
        renomination_allowed=True,
    )
    await repo.upsert_contract(contract)
    start, end = _window()
    opportunity = await contracts.admit(contract.contract_id, start, end, Decimal("50"))
    obligation = await repo.get_obligation_by_opportunity(opportunity.opportunity_id)
    assert obligation is not None
    await contracts.transition_obligation(obligation.obligation_id, "SELECTED", reason_code="R-GATE-SELECT")
    await contracts.transition_obligation(
        obligation.obligation_id, "COMMITTED", reason_code="R-COMMIT-LOCK-ENTER"
    )
    await contracts.transition_obligation(obligation.obligation_id, "DELIVERING", reason_code=None)

    point = RenominationPoint(
        renomination_point_id=uuid4(),
        contract_id=contract.contract_id,
        obligation_id=obligation.obligation_id,
        scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    async with pool_connection_from(repo) as conn:
        await conn.execute(
            """
            INSERT INTO og.renomination_point (renomination_point_id, contract_id, obligation_id, scheduled_at)
            VALUES (%s, %s, %s, %s)
            """,
            (point.renomination_point_id, point.contract_id, point.obligation_id, point.scheduled_at),
        )
        await conn.commit()

    due = await contracts.due_renomination_points()
    assert point.renomination_point_id in {p.renomination_point_id for p in due}

    updated = await contracts.exercise_renomination_point(point.renomination_point_id, "RESELECTED")
    assert updated.outcome == "RESELECTED"
    reread = await repo.get_obligation(obligation.obligation_id)
    assert reread is not None
    assert reread.state == "DELIVERING"


def pool_connection_from(repo: PgContractsRepo):  # type: ignore[no-untyped-def]
    return repo._pool.connection()


@requires_db
async def test_many_customers_admit_concurrently_without_row_collisions(wired) -> None:  # type: ignore[no-untyped-def]
    """BUILD.md S2: several customers/obligations concurrently -- concurrent admits across distinct
    contracts must not collide or cross-write each other's rows."""
    repo, _trace = wired
    services = ("HOME", "ERCOT_ENERGY", "ERCOT_AS", "DIST_DEFERRAL", "PARTNER_CAPACITY")
    seeded: list[Contract] = []
    for service in services:
        contract = Contract(
            contract_id=uuid4(),
            customer_id=uuid4(),
            service_type=service,  # type: ignore[arg-type]
            tier="T2",
            profile_ref="test-profile@1",
            start_at=datetime.now(UTC) - timedelta(days=1),
        )
        await repo.upsert_contract(contract)
        seeded.append(contract)

    start, end = _window()
    results = await asyncio.gather(
        *(contracts.admit(c.contract_id, start, end, Decimal("10")) for c in seeded),
        return_exceptions=True,
    )
    opportunities = [r for r in results if not isinstance(r, BaseException)]
    assert len(opportunities) == len(services)
    assert len({o.opportunity_id for o in opportunities}) == len(services)  # type: ignore[union-attr]

    for opportunity in opportunities:
        obligation = await repo.get_obligation_by_opportunity(opportunity.opportunity_id)  # type: ignore[union-attr]
        assert obligation is not None
        assert obligation.contract_id in {c.contract_id for c in seeded}
