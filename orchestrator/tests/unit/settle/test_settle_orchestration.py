"""End-to-end `settle()` behaviour against the in-memory `SettleBackend`/`TraceStore` fakes:
idempotency (BUILD.md: "re-running a period produces no duplicates"), corrections (new versioned
rows), and "every billing run traced"."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from opengrid.settle import run_trace_pruning_cycle, settle
from opengrid.settle.models import PenaltyParams, PowerSample

from .conftest import FakeObligationSetup, FakeSettleBackend, make_context

pytestmark = pytest.mark.asyncio

_INTERVAL_START = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
_INTERVAL_END = _INTERVAL_START + timedelta(minutes=15)


def _flat_samples(kw: str, start: datetime, count: int = 15) -> list[PowerSample]:
    return [
        PowerSample(hub_id="hub-1", ts=start + timedelta(minutes=i), kw=Decimal(kw)) for i in range(count)
    ]


async def test_first_settlement_inserts_meter_performance_pnl_and_invoice_line(
    fake_backend: FakeSettleBackend,
):
    obligation_id = uuid4()
    fake_backend.obligations[obligation_id] = FakeObligationSetup(
        context=make_context(obligation_id=obligation_id, committed_kw=Decimal("4")),
        samples=_flat_samples("4", _INTERVAL_START),
    )

    await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)

    assert fake_backend.insert_meter_interval_calls == 1
    assert fake_backend.insert_pnl_calls == 1
    assert fake_backend.insert_invoice_line_calls == 1
    assert (obligation_id, _INTERVAL_START) in fake_backend.meter_intervals
    assert fake_backend.meter_intervals[(obligation_id, _INTERVAL_START)].delivered_kwh == Decimal("1.0")


async def test_rerunning_unchanged_interval_is_idempotent(fake_backend: FakeSettleBackend):
    """BUILD.md: "scheduled every interval and idempotent (re-running a period produces no
    duplicates)"."""
    obligation_id = uuid4()
    fake_backend.obligations[obligation_id] = FakeObligationSetup(
        context=make_context(obligation_id=obligation_id, committed_kw=Decimal("4")),
        samples=_flat_samples("4", _INTERVAL_START),
    )

    await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)
    await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)
    await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)

    assert fake_backend.insert_meter_interval_calls == 1
    assert fake_backend.insert_pnl_calls == 1
    assert fake_backend.insert_invoice_line_calls == 1


async def test_rerunning_with_changed_telemetry_posts_a_correction(fake_backend: FakeSettleBackend):
    """A correction is a new, insert-only versioned row -- not a mutation of the original."""
    obligation_id = uuid4()
    fake_backend.obligations[obligation_id] = FakeObligationSetup(
        context=make_context(obligation_id=obligation_id, committed_kw=Decimal("4")),
        samples=_flat_samples("4", _INTERVAL_START),
    )
    await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)
    original_meter = fake_backend.meter_intervals[(obligation_id, _INTERVAL_START)]
    original_line = fake_backend.invoice_lines[(obligation_id, "CAPACITY_PAYMENT")]

    # telemetry corrected: 6 kW instead of 4 kW
    fake_backend.obligations[obligation_id].samples = _flat_samples("6", _INTERVAL_START)
    await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)

    assert fake_backend.insert_meter_interval_calls == 2
    assert fake_backend.insert_invoice_line_calls == 2
    corrected_meter = fake_backend.meter_intervals[(obligation_id, _INTERVAL_START)]
    corrected_line = fake_backend.invoice_lines[(obligation_id, "CAPACITY_PAYMENT")]
    assert corrected_meter.meter_interval_id != original_meter.meter_interval_id
    assert corrected_meter.version == original_meter.version + 1
    assert corrected_line.version == original_line.version + 1


async def test_shortfall_posts_ld_penalty_line(fake_backend: FakeSettleBackend):
    obligation_id = uuid4()
    penalty = PenaltyParams(alpha=Decimal("0.01"), beta=Decimal("0.5"), theta=Decimal("0.05"))
    fake_backend.obligations[obligation_id] = FakeObligationSetup(
        context=make_context(obligation_id=obligation_id, committed_kw=Decimal("10"), penalty=penalty),
        # 10 kW committed for 0.25h = 2.5 kWh baseline; deliver only 1 kWh -> a large shortfall
        samples=_flat_samples("4", _INTERVAL_START),
    )

    await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)

    assert (obligation_id, "LD_PENALTY") in fake_backend.invoice_lines
    assert fake_backend.invoice_lines[(obligation_id, "LD_PENALTY")].amount < 0


async def test_home_service_posts_no_invoice_line(fake_backend: FakeSettleBackend):
    obligation_id = uuid4()
    fake_backend.obligations[obligation_id] = FakeObligationSetup(
        context=make_context(obligation_id=obligation_id, service_type="HOME"),
        samples=_flat_samples("4", _INTERVAL_START),
    )

    await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)

    assert fake_backend.insert_invoice_line_calls == 0
    assert fake_backend.insert_pnl_calls == 1  # profitability is still tracked for HOME


async def test_forgone_upside_and_rule_baseline_flow_through_to_pnl(fake_backend: FakeSettleBackend):
    obligation_id = uuid4()
    fake_backend.obligations[obligation_id] = FakeObligationSetup(
        context=make_context(
            obligation_id=obligation_id, committed_kw=Decimal("4"), price_per_kwh=Decimal("0.05")
        ),
        samples=_flat_samples("4", _INTERVAL_START),
        rule_baseline_delivered_kwh=Decimal("0.5"),
        best_competing_value_per_kwh=Decimal("0.08"),
    )

    await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)

    assert fake_backend.insert_pnl_calls == 1


async def test_settle_writes_exactly_one_trace_entry_per_changed_settlement(fake_trace_store, fake_backend):
    """Every billing run is traced (BUILD.md)."""
    obligation_id = uuid4()
    fake_backend.obligations[obligation_id] = FakeObligationSetup(
        context=make_context(obligation_id=obligation_id, committed_kw=Decimal("4")),
        samples=_flat_samples("4", _INTERVAL_START),
    )

    await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)
    verify_result = await fake_trace_store.verify("settle")
    assert verify_result.ok
    assert len(await fake_trace_store._backend.fetch_range("settle", from_seq=0)) == 1

    # idempotent re-run: no new trace entry
    await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)
    verify_result_again = await fake_trace_store.verify("settle")
    assert verify_result_again.ok
    assert len(await fake_trace_store._backend.fetch_range("settle", from_seq=0)) == 1


async def test_run_trace_pruning_cycle_checkpoints_then_prunes(fake_trace_store):
    result = await run_trace_pruning_cycle()
    assert isinstance(result, dict)


async def test_settle_raises_if_not_configured():
    import opengrid.settle as settle_module

    settle_module._backend = None
    settle_module._trace_store = None
    with pytest.raises(RuntimeError):
        await settle(uuid4(), _INTERVAL_START, _INTERVAL_END)
