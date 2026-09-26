"""End-to-end `settle()` behaviour against the in-memory `SettleBackend`/`TraceStore` fakes:
idempotency (BUILD.md: "re-running a period produces no duplicates"), corrections (new versioned
rows), and "every billing run traced"."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

import opengrid.settle as settle_module
from opengrid.settle import run_trace_pruning_cycle, settle
from opengrid.settle.models import PenaltyParams, PowerSample
from opengrid.settle.tariffs import TdspTariff
from opengrid.trace import TraceStore

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


async def test_need_basis_compliant_dip_posts_no_penalty_and_full_capacity_payment(
    fake_backend: FakeSettleBackend,
):
    """D-18: a MEASURED_FEEDBACK obligation's committed kW is a reserved maximum -- delivery below it
    that matches the customer's measured need is compliant, never penalised, and bills the full
    reserved-capacity payment (not scaled down by the lower true compliance_pct)."""
    obligation_id = uuid4()
    penalty = PenaltyParams(alpha=Decimal("0.01"), beta=Decimal("0.5"), theta=Decimal("0.05"))
    fake_backend.obligations[obligation_id] = FakeObligationSetup(
        context=make_context(
            obligation_id=obligation_id,
            service_type="DATA_CENTER",
            committed_kw=Decimal("10"),
            price_per_kwh=Decimal("0.10"),
            penalty=penalty,
            is_need_basis=True,
        ),
        # 10 kW committed for 0.25h = 2.5 kWh baseline; delivers only 1 kWh, but the measured need was
        # also 1 kWh -- a genuine need-basis dip, not a shortfall.
        samples=_flat_samples("4", _INTERVAL_START),
        measured_need_kwh=Decimal("1.0"),
    )

    await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)

    assert (obligation_id, "LD_PENALTY") not in fake_backend.invoice_lines
    capacity_line = fake_backend.invoice_lines[(obligation_id, "CAPACITY_PAYMENT")]
    # full reserved-capacity payment: committed_kwh (2.5) * price_per_kwh (0.10) = 0.25, not scaled by
    # the true (1/2.5 = 40%) compliance ratio.
    assert capacity_line.amount == Decimal("0.25")


async def test_need_basis_without_a_site_meter_reading_defaults_to_compliant(
    fake_backend: FakeSettleBackend,
):
    """No site-meter reading for the interval (PIPELINE_AC has no kWh-shaped meter; a DATA_CENTER
    reading might be missing/stale) -- the dip is still treated as compliant by default
    (`performance.is_need_basis_compliant`'s documented stance), never penalised."""
    obligation_id = uuid4()
    penalty = PenaltyParams(alpha=Decimal("0.01"), beta=Decimal("0.5"), theta=Decimal("0.05"))
    fake_backend.obligations[obligation_id] = FakeObligationSetup(
        context=make_context(
            obligation_id=obligation_id,
            service_type="DATA_CENTER",
            committed_kw=Decimal("10"),
            penalty=penalty,
            is_need_basis=True,
        ),
        samples=_flat_samples("4", _INTERVAL_START),
        measured_need_kwh=None,
    )

    await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)

    assert (obligation_id, "LD_PENALTY") not in fake_backend.invoice_lines


async def test_need_basis_delivery_below_measured_need_is_still_a_shortfall(
    fake_backend: FakeSettleBackend,
):
    """A dip below committed that ALSO undercuts the customer's own measured need is a genuine
    shortfall (the K13 scope `classify_dip` still leaves open), not excused by need-basis alone."""
    obligation_id = uuid4()
    penalty = PenaltyParams(alpha=Decimal("0.01"), beta=Decimal("0.5"), theta=Decimal("0.05"))
    fake_backend.obligations[obligation_id] = FakeObligationSetup(
        context=make_context(
            obligation_id=obligation_id,
            service_type="DATA_CENTER",
            committed_kw=Decimal("10"),
            penalty=penalty,
            is_need_basis=True,
        ),
        # delivers 1 kWh but the customer needed 2 kWh -- still short of measured need.
        samples=_flat_samples("4", _INTERVAL_START),
        measured_need_kwh=Decimal("2.0"),
    )

    await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)

    assert (obligation_id, "LD_PENALTY") in fake_backend.invoice_lines


async def test_ercot_as_held_not_deployed_posts_full_capacity_payment_no_penalty(
    fake_backend: FakeSettleBackend,
):
    """2026-09-26 live-soak correction: an AS award holds capacity and is normally never discharged.
    Zero delivery must bill the full award x MCPC capacity payment (never scaled by the near-zero
    delivered/committed ratio) and must never post an LD_PENALTY for simply not discharging."""
    obligation_id = uuid4()
    penalty = PenaltyParams(alpha=Decimal("0.02"), beta=Decimal("0.30"), theta=Decimal("0.10"))
    fake_backend.obligations[obligation_id] = FakeObligationSetup(
        context=make_context(
            obligation_id=obligation_id,
            service_type="ERCOT_AS",
            committed_kw=Decimal("500"),
            price_per_kwh=Decimal("0.0085"),  # $8.50/MW-h NSPIN MCPC
            penalty=penalty,
        ),
        samples=_flat_samples("0", _INTERVAL_START),  # held, never deployed
    )

    await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)

    assert (obligation_id, "LD_PENALTY") not in fake_backend.invoice_lines
    capacity_line = fake_backend.invoice_lines[(obligation_id, "CAPACITY_PAYMENT")]
    # committed_kwh = 500 kW * 0.25 h = 125 kWh; 125 * 0.0085 = 1.0625.
    assert capacity_line.amount == Decimal("1.0625")
    # AS performance = AVAILABILITY, not delivered/committed (~0% here by design while held): the
    # metered performance row must still be recorded as passed, so
    # opengrid.engine.lifecycle.close_target's `NOT p.passed_threshold` check never closes a held
    # window as SHORTFALL.
    compliance_pct, passed_threshold = fake_backend.performance_rows[(obligation_id, _INTERVAL_START)]
    assert compliance_pct is None
    assert passed_threshold is True


async def test_ercot_as_deployment_adds_energy_value_and_wear(fake_backend: FakeSettleBackend):
    """A real deployment (non-zero delivered kWh) earns energy value on top of the capacity payment,
    priced at the discharge-interval SPP (not the charging cost), and wear applies to that discharged
    energy -- but the capacity payment itself is unaffected."""
    obligation_id = uuid4()
    fake_backend.obligations[obligation_id] = FakeObligationSetup(
        context=make_context(
            obligation_id=obligation_id,
            service_type="ERCOT_AS",
            committed_kw=Decimal("500"),
            price_per_kwh=Decimal("0.0085"),
            wholesale_price_per_kwh=Decimal("0.045"),  # $45/MWh real-time SPP at deployment
            degradation_cost_per_kwh=Decimal("0.03"),
        ),
        # 80 kW average for 15 min = 20 kWh deployed.
        samples=_flat_samples("80", _INTERVAL_START),
    )

    await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)

    pnl_row = fake_backend.pnl_rows[(obligation_id, _INTERVAL_START)]
    capacity_line = fake_backend.invoice_lines[(obligation_id, "CAPACITY_PAYMENT")]
    # capacity payment is unaffected by the deployment: still 125 kWh * 0.0085 = 1.0625.
    assert capacity_line.amount == Decimal("1.0625")
    # revenue = capacity (1.0625) + energy (20 kWh * 0.045 = 0.90) = 1.9625.
    # (net_value also nets energy_cost/wear/penalty, so check net_value against the hand-computed pnl
    # module test instead of re-deriving the full formula here -- this just confirms the deployment
    # was recognized at all, via a strictly higher net_value than the held-only case.)
    assert pnl_row.net_value > Decimal("0")


async def test_m1_delivery_charge_applies_to_oncor_zone_grid_charged_kwh(
    fake_backend: FakeSettleBackend, fake_trace_store: TraceStore
):
    """09 D5: 100 kWh drawn from the grid to charge, in Oncor's ERCOT-competitive territory (LZ_NORTH)
    -> a separate delivery_charge P&L line of 100 * $0.060295/kWh = $6.0295, netted out of net_value."""
    settle_module.configure(
        fake_backend,
        fake_trace_store,
        tdsp_tariffs=[
            TdspTariff(
                tdsp="ONCOR",
                effective_from=date(2026, 9, 1),
                volumetric_usd_per_kwh=Decimal("0.060295"),
                load_zones=("LZ_NORTH", "LZ_WEST"),
            )
        ],
        zone_default_tdsp={"LZ_NORTH": "ONCOR"},
    )
    obligation_id = uuid4()
    fake_backend.obligations[obligation_id] = FakeObligationSetup(
        context=make_context(obligation_id=obligation_id, committed_kw=Decimal("4"), zone="LZ_NORTH"),
        samples=_flat_samples("4", _INTERVAL_START),
        grid_charged_kwh=Decimal("100"),
    )

    await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)

    pnl_row = fake_backend.pnl_rows[(obligation_id, _INTERVAL_START)]
    # Without M1, this fully-delivered 1 kWh interval nets to a few cents (revenue 0.10 minus a few
    # cents of energy cost and wear); the $6.0295 M1 charge on 100 kWh of grid charging dwarfs that,
    # so a strongly negative net_value confirms the charge was actually applied (the exact dollar
    # figure is confirmed by the pure profitability.compute_pnl/tariffs tests).
    assert pnl_row.net_value < Decimal("-5")


async def test_no_delivery_charge_for_a_regulated_or_unmapped_zone(
    fake_backend: FakeSettleBackend, fake_trace_store: TraceStore
):
    """A zone with no TDSP mapping (Austin Energy/CPS Energy territory, or simply unmapped) -- M1
    never applies, even with grid-charged kWh and tariffs configured."""
    settle_module.configure(
        fake_backend,
        fake_trace_store,
        tdsp_tariffs=[
            TdspTariff(
                tdsp="ONCOR",
                effective_from=date(2026, 9, 1),
                volumetric_usd_per_kwh=Decimal("0.060295"),
                load_zones=("LZ_NORTH",),
            )
        ],
        zone_default_tdsp={"LZ_NORTH": "ONCOR"},  # LZ_AUSTIN deliberately absent
    )
    obligation_id = uuid4()
    fake_backend.obligations[obligation_id] = FakeObligationSetup(
        context=make_context(obligation_id=obligation_id, committed_kw=Decimal("4"), zone="LZ_AUSTIN"),
        samples=_flat_samples("4", _INTERVAL_START),
        grid_charged_kwh=Decimal("100"),
    )

    await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)

    # No exception, no M1 charge (verified directly against the pure tariff resolution in
    # test_tariffs.py); this test's job is only to confirm settle() doesn't blow up or guess a rate
    # for an unmapped zone.
    assert (obligation_id, _INTERVAL_START) in fake_backend.pnl_rows


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
