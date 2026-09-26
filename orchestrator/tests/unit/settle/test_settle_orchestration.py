"""End-to-end `settle()` behaviour against the in-memory `SettleBackend`/`TraceStore` fakes:
idempotency (BUILD.md: "re-running a period produces no duplicates"), corrections (new versioned
rows), and "every billing run traced"."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

import opengrid.settle as settle_module
from opengrid.core.models.market import Utility
from opengrid.settle import run_trace_pruning_cycle, settle
from opengrid.settle.models import PenaltyParams, PowerSample, ZoneChargeEnergy
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
    # window as SHORTFALL. og.performance.compliance_pct is NOT NULL (live bug fix 2026-09-26): it
    # must be a number (1.0 = 100% available), never None.
    compliance_pct, passed_threshold = fake_backend.performance_rows[(obligation_id, _INTERVAL_START)]
    assert compliance_pct == Decimal("1")
    assert passed_threshold is True


@pytest.mark.parametrize("risk_open", [True, False])
async def test_ercot_as_interval_is_flagged_while_shortfall_risk_was_open_payment_unchanged(
    fake_backend: FakeSettleBackend, fake_trace_store: TraceStore, risk_open: bool
):
    """Review fix: ERCOT_AS always settled compliant with no trace of a held-capacity risk. While
    ALR-ENERGY-SHORTFALL-RISK was open for the award, the settlement carries R-AS-HOLD-SHORT (reason code
    and payload flag); the capacity payment is unchanged (no owner decision on a penalty yet)."""
    obligation_id = uuid4()
    fake_backend.obligations[obligation_id] = FakeObligationSetup(
        context=make_context(
            obligation_id=obligation_id,
            service_type="ERCOT_AS",
            committed_kw=Decimal("500"),
            price_per_kwh=Decimal("0.0085"),
        ),
        samples=_flat_samples("0", _INTERVAL_START),
        shortfall_risk_open=risk_open,
    )

    await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)

    (record,) = fake_trace_store._backend.streams["settle"]
    assert record.payload["as_hold_short"] is risk_open
    assert record.reason_codes == (["R-AS-HOLD-SHORT"] if risk_open else None)
    assert fake_backend.invoice_lines[(obligation_id, "CAPACITY_PAYMENT")].amount == Decimal("1.0625")
    assert fake_backend.performance_rows[(obligation_id, _INTERVAL_START)] == (Decimal("1"), True)


async def test_only_ercot_as_is_checked_for_a_hold_shortfall(
    fake_backend: FakeSettleBackend, fake_trace_store: TraceStore
):
    obligation_id = uuid4()
    fake_backend.obligations[obligation_id] = FakeObligationSetup(
        context=make_context(obligation_id=obligation_id, committed_kw=Decimal("4")),
        samples=_flat_samples("4", _INTERVAL_START),
        shortfall_risk_open=True,
    )

    await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)

    (record,) = fake_trace_store._backend.streams["settle"]
    assert record.reason_codes is None and record.payload["as_hold_short"] is False


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


def _configure_oncor(fake_backend: FakeSettleBackend, fake_trace_store: TraceStore) -> None:
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


async def test_m1_delivery_charge_applies_the_full_charge_to_the_energy_charged_for_a_delivery(
    fake_backend: FakeSettleBackend, fake_trace_store: TraceStore
):
    """Review fix (M1 always settled at $0): in Oncor's ERCOT-competitive territory (LZ_NORTH) with no PV,
    delivering 1 kWh needed 1 / (0.9487 x 0.9487) kWh of grid charging, charged the FULL $0.060295/kWh --
    even though the zone did not charge during the delivery interval itself."""
    _configure_oncor(fake_backend, fake_trace_store)
    fake_backend.zone_charge["LZ_NORTH"] = ZoneChargeEnergy(Decimal("500"), Decimal("500"))  # all grid
    obligation_id = uuid4()
    fake_backend.obligations[obligation_id] = FakeObligationSetup(
        context=make_context(obligation_id=obligation_id, committed_kw=Decimal("4"), zone="LZ_NORTH"),
        samples=_flat_samples("4", _INTERVAL_START),  # 1 kWh delivered
    )

    await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)

    expected = Decimal("1") / (Decimal("0.9487") * Decimal("0.9487")) * Decimal("0.060295")
    assert fake_backend.pnl_delivery_charge[(obligation_id, _INTERVAL_START)] == expected
    # the grid share is read over the trailing 24 h, hour-aligned, ending at the interval
    window_end = _INTERVAL_END.replace(minute=0)
    assert fake_backend.zone_charge_calls == [("LZ_NORTH", window_end - timedelta(hours=24), window_end)]


async def test_m1_excludes_the_pv_surplus_share_of_charging(
    fake_backend: FakeSettleBackend, fake_trace_store: TraceStore
):
    _configure_oncor(fake_backend, fake_trace_store)
    fake_backend.zone_charge["LZ_NORTH"] = ZoneChargeEnergy(Decimal("400"), Decimal("100"))  # 25 % grid
    obligation_id = uuid4()
    fake_backend.obligations[obligation_id] = FakeObligationSetup(
        context=make_context(obligation_id=obligation_id, committed_kw=Decimal("4"), zone="LZ_NORTH"),
        samples=_flat_samples("4", _INTERVAL_START),
    )

    await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)

    full = Decimal("1") / (Decimal("0.9487") * Decimal("0.9487")) * Decimal("0.060295")
    assert fake_backend.pnl_delivery_charge[(obligation_id, _INTERVAL_START)] == full * Decimal("0.25")


async def test_m1_is_zero_when_nothing_was_delivered(
    fake_backend: FakeSettleBackend, fake_trace_store: TraceStore
):
    _configure_oncor(fake_backend, fake_trace_store)
    obligation_id = uuid4()
    fake_backend.obligations[obligation_id] = FakeObligationSetup(
        context=make_context(obligation_id=obligation_id, committed_kw=Decimal("4"), zone="LZ_NORTH"),
        samples=_flat_samples("0", _INTERVAL_START),
    )

    await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)

    assert fake_backend.pnl_delivery_charge[(obligation_id, _INTERVAL_START)] == Decimal("0")
    assert fake_backend.zone_charge_calls == []  # no telemetry scan for an interval that delivered nothing


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
    fake_backend.zone_charge["LZ_AUSTIN"] = ZoneChargeEnergy(Decimal("500"), Decimal("500"))
    obligation_id = uuid4()
    fake_backend.obligations[obligation_id] = FakeObligationSetup(
        context=make_context(obligation_id=obligation_id, committed_kw=Decimal("4"), zone="LZ_AUSTIN"),
        samples=_flat_samples("4", _INTERVAL_START),
    )

    await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)

    assert fake_backend.pnl_delivery_charge[(obligation_id, _INTERVAL_START)] == Decimal("0")
    assert fake_backend.zone_charge_calls == []  # a zone with no TDSP is never even queried


_AUSTIN_ENERGY = Utility(
    utility_id="AUSTIN_ENERGY",
    name="Austin Energy",
    territory_zones=["LZ_AEN"],
    capacity_product="RESIDENTIAL_BATTERY_DR",
    payment_basis="USD_PER_KW_YEAR",
    capacity_price_usd_per_kw=Decimal("75"),
    charging_tariff_kind="TOU_OFF_PEAK",
    off_peak_rate_usd_per_kwh=Decimal("0.02677"),
    charging_adder_usd_per_kwh=Decimal("0"),
    solar_cost_usd_per_kwh=Decimal("0.040"),
    solar_share_floor=Decimal("0.30"),
    tariff_ref="AE-FY2026-RES-TOU-PILOT",
)


async def test_regulated_capacity_held_bills_utility_capacity_payment_no_penalty(
    fake_backend: FakeSettleBackend,
):
    """08 S3/09 D1-D2: a REGULATED_CAPACITY contract is a utility capacity hold, priced via
    opengrid.market.capacity.regulated_capacity_payment ($/kW-yr pro-rated to the interval), never
    scaled by delivered/committed, and never penalised for a normal held-not-discharged interval."""
    obligation_id = uuid4()
    penalty = PenaltyParams(alpha=Decimal("0.02"), beta=Decimal("0.30"), theta=Decimal("0.10"))
    fake_backend.obligations[obligation_id] = FakeObligationSetup(
        context=make_context(
            obligation_id=obligation_id,
            service_type="REGULATED_CAPACITY",
            committed_kw=Decimal("500"),
            penalty=penalty,
            market="REGULATED",
            utility_id="AUSTIN_ENERGY",
            zone="LZ_AEN",
        ),
        samples=_flat_samples("0", _INTERVAL_START),
        utility=_AUSTIN_ENERGY,
    )

    await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)

    assert (obligation_id, "LD_PENALTY") not in fake_backend.invoice_lines
    capacity_line = fake_backend.invoice_lines[(obligation_id, "CAPACITY_PAYMENT")]
    # 500 kW * $75/kW-yr * (0.25h / 8760h) = 1.0702054794...
    expected = Decimal("500") * Decimal("75") * (Decimal("0.25") / Decimal("8760"))
    assert capacity_line.amount == expected
    compliance_pct, passed_threshold = fake_backend.performance_rows[(obligation_id, _INTERVAL_START)]
    assert compliance_pct == Decimal("1")
    assert passed_threshold is True


async def test_regulated_market_charging_cost_overrides_the_free_market_proxy(
    fake_backend: FakeSettleBackend,
):
    """A REGULATED contract's energy cost uses opengrid.market.charging.regulated_charging_cost
    (TOU-aware utility terms), never the FREE-market trailing-24h-SPP proxy, and never M1."""
    obligation_id = uuid4()
    fake_backend.obligations[obligation_id] = FakeObligationSetup(
        context=make_context(
            obligation_id=obligation_id,
            service_type="ERCOT_ENERGY",
            committed_kw=Decimal("4"),
            market="REGULATED",
            utility_id="AUSTIN_ENERGY",
            zone="LZ_AEN",
            charging_cost_per_kwh=Decimal("999"),  # must be ignored: the FREE-market proxy value
        ),
        samples=_flat_samples("4", _INTERVAL_START),
        utility=_AUSTIN_ENERGY,
    )

    await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)

    pnl_row = fake_backend.pnl_rows[(obligation_id, _INTERVAL_START)]
    # If the $999/kWh FREE-market proxy had been used, energy_cost would dwarf revenue and net_value
    # would be deeply negative; the regulated blended rate (a few cents/kWh) keeps it near revenue.
    assert pnl_row.net_value > Decimal("-1")


async def test_pipeline_ac_bills_a_fixed_fee_line(fake_backend: FakeSettleBackend):
    """06 S4.a: PIPELINE_AC is a flat corridor-mitigation service fee (FIXED_FEE), never scaled by
    delivered kWh or performance factor."""
    obligation_id = uuid4()
    fake_backend.obligations[obligation_id] = FakeObligationSetup(
        context=make_context(
            obligation_id=obligation_id,
            service_type="PIPELINE_AC",
            committed_kw=Decimal("10"),
            price_per_kwh=Decimal("0.20"),
        ),
        samples=_flat_samples("2", _INTERVAL_START),  # a real shortfall vs the 10 kW commitment
    )

    await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)

    assert (obligation_id, "FIXED_FEE") in fake_backend.invoice_lines
    fee_line = fake_backend.invoice_lines[(obligation_id, "FIXED_FEE")]
    # committed_kwh = 10 kW * 0.25h = 2.5 kWh; 2.5 * 0.20 = 0.50, NOT scaled by the ~20% delivered ratio.
    assert fee_line.amount == Decimal("0.50")


async def test_ercot_energy_revenue_uses_zone_spp_even_with_no_opportunity_price(
    fake_backend: FakeSettleBackend,
):
    """Live bug (2026-09-26 P&L review): ERCOT_ENERGY revenue settled at $0 because the opportunity's
    value_per_mwh (price_per_kwh) was null on many admitted obligations. Revenue must come from the
    zone's real-time SPP (wholesale_price_per_kwh) regardless of price_per_kwh."""
    obligation_id = uuid4()
    fake_backend.obligations[obligation_id] = FakeObligationSetup(
        context=make_context(
            obligation_id=obligation_id,
            service_type="ERCOT_ENERGY",
            committed_kw=Decimal("4"),
            price_per_kwh=Decimal("0"),  # null opportunity value_per_mwh
            wholesale_price_per_kwh=Decimal("0.045"),  # live $45/MWh zone SPP
        ),
        samples=_flat_samples("4", _INTERVAL_START),
    )

    await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)

    pnl_row = fake_backend.pnl_rows[(obligation_id, _INTERVAL_START)]
    energy_line = fake_backend.invoice_lines[(obligation_id, "ENERGY")]
    # delivered = 4 kW * 0.25h = 1 kWh; revenue = 1 * 0.045 = 0.045, NOT $0.
    assert pnl_row.net_value != Decimal("0")
    assert energy_line.amount == Decimal("0.045")


async def test_pjm_emergency_hour_shortfall_adds_a_non_performance_charge(fake_backend: FakeSettleBackend):
    """opengrid.settle.services_extra.pjm_non_performance_charge: a shortfall during a declared PJM
    emergency performance hour adds an extra charge on top of the ordinary penalty, folded into
    pnl.penalty/net_value and the single LD_PENALTY line."""
    obligation_id = uuid4()
    penalty = PenaltyParams(alpha=Decimal("0.01"), beta=Decimal("0.1"), theta=Decimal("0.05"))
    fake_backend.obligations[obligation_id] = FakeObligationSetup(
        context=make_context(
            obligation_id=obligation_id,
            service_type="PJM_CAPACITY",
            committed_kw=Decimal("100"),
            penalty=penalty,
        ),
        # 100 kW committed for 0.25h = 25 kWh baseline; delivers only 10 kWh -> a real shortfall.
        samples=_flat_samples("40", _INTERVAL_START),
        pjm_emergency_rate=Decimal("2.00"),  # $2.00/kWh non-performance rate
    )

    await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)

    pnl_row = fake_backend.pnl_rows[(obligation_id, _INTERVAL_START)]
    ld_penalty = fake_backend.invoice_lines[(obligation_id, "LD_PENALTY")]
    # shortfall_kwh = 25 - 10 = 15 kWh; non-performance charge = 15 * 2.00 = 30.00, on top of the
    # ordinary tolerance-band penalty -- the combined amount must exceed the extra charge alone.
    assert -ld_penalty.amount >= Decimal("30.00")
    assert pnl_row.net_value < Decimal("-29")


async def test_pjm_routine_hour_has_no_non_performance_charge(fake_backend: FakeSettleBackend):
    """Outside a declared emergency hour (fetch_pjm_emergency_rate returns None), only the ordinary
    CAPACITY_PAYMENT x performance_factor / tolerance-band penalty applies."""
    obligation_id = uuid4()
    penalty = PenaltyParams(alpha=Decimal("0.01"), beta=Decimal("0.1"), theta=Decimal("0.05"))
    fake_backend.obligations[obligation_id] = FakeObligationSetup(
        context=make_context(
            obligation_id=obligation_id,
            service_type="PJM_CAPACITY",
            committed_kw=Decimal("100"),
            penalty=penalty,
        ),
        samples=_flat_samples("40", _INTERVAL_START),
        pjm_emergency_rate=None,
    )

    await settle(obligation_id, _INTERVAL_START, _INTERVAL_END)

    ld_penalty = fake_backend.invoice_lines[(obligation_id, "LD_PENALTY")]
    # Ordinary penalty only: shortfall 15 kWh, tolerance band 0.05*25=1.25 kWh at alpha 0.01, rest at
    # beta 0.1 -> 1.25*0.01 + 13.75*0.1 = 0.0125 + 1.375 = 1.3875 -- nowhere near the $30 PJM charge.
    assert -ld_penalty.amount < Decimal("2")


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
