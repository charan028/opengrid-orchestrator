"""`run_settle_cycle`: many obligations settled concurrently (BUILD.md S2), one failure does not
stop the batch (K7: degrade, don't trip)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from opengrid.settle import run_settle_cycle
from opengrid.settle.models import PowerSample

from .conftest import FakeObligationSetup, FakeSettleBackend, make_context

pytestmark = pytest.mark.asyncio

_INTERVAL_START = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
_INTERVAL_END = _INTERVAL_START + timedelta(minutes=15)


def _samples() -> list[PowerSample]:
    return [
        PowerSample(hub_id="hub-1", ts=_INTERVAL_START + timedelta(minutes=i), kw=Decimal("4"))
        for i in range(15)
    ]


async def test_run_settle_cycle_settles_every_pending_interval(fake_backend: FakeSettleBackend):
    obligation_ids = [uuid4() for _ in range(5)]
    for obligation_id in obligation_ids:
        fake_backend.obligations[obligation_id] = FakeObligationSetup(
            context=make_context(obligation_id=obligation_id, committed_kw=Decimal("4")),
            samples=_samples(),
        )
        fake_backend.pending.append((obligation_id, _INTERVAL_START, _INTERVAL_END))

    settled_count = await run_settle_cycle(max_concurrency=2)

    assert settled_count == 5
    assert fake_backend.insert_pnl_calls == 5


async def test_run_settle_cycle_skips_a_failing_obligation_but_settles_the_rest(
    fake_backend: FakeSettleBackend,
):
    good_id, bad_id = uuid4(), uuid4()
    fake_backend.obligations[good_id] = FakeObligationSetup(
        context=make_context(obligation_id=good_id, committed_kw=Decimal("4")),
        samples=_samples(),
    )
    # `bad_id` deliberately has no registered context -> fetch_context raises KeyError inside settle()
    fake_backend.pending = [
        (bad_id, _INTERVAL_START, _INTERVAL_END),
        (good_id, _INTERVAL_START, _INTERVAL_END),
    ]

    settled_count = await run_settle_cycle()

    assert settled_count == 1
    assert fake_backend.insert_pnl_calls == 1


async def test_run_settle_cycle_with_no_pending_intervals_settles_nothing(fake_backend: FakeSettleBackend):
    assert await run_settle_cycle() == 0
