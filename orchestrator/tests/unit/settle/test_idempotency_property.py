"""Property test: re-running `settle()` for the same obligation-interval any number of times with
unchanged telemetry never creates a duplicate active row (BUILD.md: "idempotent -- re-running a
period produces no duplicates"; TS-08-01's "no kWh billed twice" spirit applied to settle's own
insert-only tables)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from opengrid.settle import settle
from opengrid.settle.models import PowerSample

from .conftest import FakeObligationSetup, FakeSettleBackend, make_context

pytestmark = pytest.mark.asyncio

_INTERVAL_START = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
_INTERVAL_END = _INTERVAL_START + timedelta(minutes=15)

_kw_values = st.lists(
    st.decimals(min_value=Decimal("0"), max_value=Decimal("11"), places=3),
    min_size=0,
    max_size=15,
)
_repeat_counts = st.integers(min_value=1, max_value=5)


@given(kw_values=_kw_values, repeat_count=_repeat_counts)
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
async def test_repeated_settle_calls_never_duplicate_rows(
    kw_values: list[Decimal], repeat_count: int, fake_backend: FakeSettleBackend
):
    obligation_id = uuid4()
    samples = [
        PowerSample(hub_id="hub-1", ts=_INTERVAL_START + timedelta(minutes=i), kw=kw)
        for i, kw in enumerate(kw_values)
    ]
    fake_backend.obligations[obligation_id] = FakeObligationSetup(
        context=make_context(obligation_id=obligation_id, committed_kw=Decimal("4")),
        samples=samples,
    )

    for _ in range(repeat_count):
        await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)

    # exactly one active meter_interval / pnl row exists per obligation-interval, regardless of how
    # many times settle() ran, because the telemetry never changed between calls.
    # A fresh obligation_id per example lets these per-key counts stay valid even though the fixture
    # (function-scoped-fixture health check suppressed) is shared across Hypothesis examples: no
    # matter how many of `repeat_count` calls ran, exactly one insert happened for THIS key.
    key = (obligation_id, _INTERVAL_START)
    assert fake_backend.meter_interval_insert_log.count(key) == 1
    assert fake_backend.pnl_insert_log.count(key) == 1
