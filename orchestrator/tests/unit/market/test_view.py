"""The $/kW profitability view payload and its settled-data mapping."""

from __future__ import annotations

from decimal import Decimal

from opengrid.market.economics import PeriodTotals
from opengrid.market.pg_backend import PLANNING_NOTE, contract_totals_from_row
from opengrid.market.view import profitability_per_kw

MONTH = Decimal("730")


def _scope(ref: str, market: str | None, kw: str, revenue: str) -> PeriodTotals:
    return PeriodTotals(
        scope_kind="CONTRACT",
        scope_ref=ref,
        market=market,  # type: ignore[arg-type]
        kw_basis=Decimal(kw),
        hours=MONTH,
        capacity_revenue_usd=Decimal(revenue),
        capex_usd=Decimal(kw) * Decimal("636"),
    )


def test_per_kw_summary_rolls_up_by_market_and_fleet() -> None:
    summary = profitability_per_kw(
        [
            _scope("reg", "REGULATED", "2000", "12500"),
            _scope("free", "FREE", "500", "1000"),
            _scope("x", None, "1", "0"),
        ]
    )
    assert [c.scope_ref for c in summary.contracts] == ["reg", "free", "x"]
    assert summary.markets["REGULATED"].kw_basis == Decimal("2000")
    assert summary.markets["FREE"].out_usd_per_kw_yr == Decimal("1000") * Decimal("12") / Decimal("500")
    assert summary.fleet.kw_basis == Decimal("2501")
    assert summary.period_hours == MONTH
    assert round(summary.hardware_view_usd_per_kw, 2) == Decimal("636.36")
    assert summary.illustrative_home_unit.payback_years is not None
    body = summary.model_dump(mode="json")
    assert set(body) >= {"contracts", "markets", "fleet", "illustrative_home_unit", "target_payback_years"}


def test_empty_view() -> None:
    summary = profitability_per_kw([])
    assert summary.contracts == []
    assert summary.period_hours is None
    assert summary.fleet.kw_basis == 0


def _row(**kw: object) -> dict[str, object]:
    row: dict[str, object] = {
        "contract_id": "c1",
        "service_type": "REGULATED_CAPACITY",
        "market": "REGULATED",
        "utility_id": "AUSTIN_ENERGY",
        "revenue": Decimal("12500"),
        "energy_cost": Decimal("900"),
        "degradation_cost": Decimal("50"),
        "penalty": Decimal("10"),
        "delivery_charge": Decimal("0"),
        "kwh_held": Decimal("2000") * Decimal("90"),
        "hours_held": Decimal("90"),
    }
    row.update(kw)
    return row


def test_settled_row_maps_to_period_totals() -> None:
    totals = contract_totals_from_row(_row(), MONTH)
    assert totals.market == "REGULATED"
    assert totals.kw_basis == Decimal("2000")
    assert totals.capacity_revenue_usd == Decimal("12500")
    assert totals.energy_revenue_usd == 0
    assert totals.charging_energy_usd == Decimal("900")
    assert totals.wear_usd == Decimal("50")
    assert totals.penalty_usd == Decimal("10")
    assert totals.capex_usd == Decimal("2000") * Decimal("7000") / Decimal("11")
    assert PLANNING_NOTE in totals.notes


def test_energy_service_revenue_is_energy_and_bad_market_is_fleet_only() -> None:
    free = contract_totals_from_row(
        _row(service_type="ERCOT_ENERGY", market="FREE", utility_id=None, delivery_charge=Decimal("60")),
        MONTH,
    )
    assert free.market == "FREE"
    assert free.energy_revenue_usd == Decimal("12500") and free.capacity_revenue_usd == 0
    assert free.delivery_charge_usd == Decimal("60")
    bad = contract_totals_from_row(_row(market="FREE"), MONTH)
    assert bad.market is None
    assert any(n.startswith("market unresolved") for n in bad.notes)
    idle = contract_totals_from_row(_row(hours_held=0, kwh_held=0), MONTH)
    assert idle.kw_basis == 0
