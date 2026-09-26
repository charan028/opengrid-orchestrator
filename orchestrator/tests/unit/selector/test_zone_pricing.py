"""Architect finding (a): the selector priced EVERY bank at LZ_WEST -- `load_scenarios` kept the last row
per interval regardless of series or kind, folding load-forecast (MW) rows into the price path. Each bank
must be priced at its own load zone; load rows never enter a price path."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from opengrid.forecast.models import ScenarioPoint
from opengrid.selector.gate import scenarios_from_points

H0 = datetime(2026, 9, 26, 17, 0, tzinfo=UTC)


def _p(series: str, t: int, value: float, kind: str = "price", scenario: str = "P50") -> ScenarioPoint:
    return ScenarioPoint(
        scenario=scenario,  # type: ignore[arg-type]
        probability=0.5,
        interval_start=H0 + timedelta(minutes=15 * t),
        value=value,
        series_key=series,
        kind=kind,  # type: ignore[arg-type]
    )


def test_each_bank_gets_its_own_zone_and_load_rows_are_ignored():
    points = [
        _p("LZ_NORTH", 0, 20.0),
        _p("LZ_HOUSTON", 0, 60.0),
        _p("LZ_WEST", 0, 35.0),
        _p("LZ_WEST", 0, 48_000.0, kind="load"),  # MW, written last: must never become a price
    ]
    (scenario,) = scenarios_from_points(
        points, H0, {"bank-000": "LZ_NORTH", "bank-001": "LZ_HOUSTON", "bank-009": "LZ_SOUTH"}
    )

    assert scenario.price_at("bank-000", 0) == 20.0
    assert scenario.price_at("bank-001", 0) == 60.0
    # No LZ_SOUTH path: the fleet path is the mean of the zones seen, not whichever came last.
    assert scenario.price_at("bank-009", 0) == pytest.approx((20.0 + 60.0 + 35.0) / 3)
    assert scenario.price_at("bank-000", 5) == 0.0  # no price at all for that interval


def test_regulated_zones_are_priced_at_their_own_series_in_every_scenario():
    """LZ_AEN / LZ_CPS banks (Austin Energy, CPS Energy) get their own settlement-zone path in each of
    P10/P50/P90 -- never the competitive zones' price, never a load row."""
    points = [
        _p("LZ_AEN", 0, 22.0, scenario="P10"),
        _p("LZ_AEN", 0, 31.0, scenario="P50"),
        _p("LZ_CPS", 0, 27.0, scenario="P50"),
        _p("LZ_WEST", 0, 90.0, scenario="P50"),
        _p("LZ_AEN", 0, 12_000.0, kind="load", scenario="P50"),
    ]
    by_name = {
        s.scenario: s for s in scenarios_from_points(points, H0, {"bank-aen": "LZ_AEN", "bank-cps": "LZ_CPS"})
    }

    assert by_name["P10"].price_at("bank-aen", 0) == 22.0
    assert by_name["P50"].price_at("bank-aen", 0) == 31.0
    assert by_name["P50"].price_at("bank-cps", 0) == 27.0
    assert "bank-cps" not in by_name["P10"].price_by_bank  # no P10 CPS path: fleet fallback, not a guess


def test_not_for_firm_series_withholds_only_the_banks_priced_off_it():
    """02b S6.5: selection may not use a series forecast flags NOT_FOR_FIRM. A bank in that zone is
    withheld; a bank on a fit series is kept; a bank with no path of its own is priced at the fleet mean
    (which contains the unfit series) and is withheld too -- but only while some series is unfit."""
    from dataclasses import replace

    from opengrid.selector.gate import withhold_unfit_series
    from opengrid.selector.types import BankSnapshot
    from unit.selector.factories import binary_candidate

    points = [_p("LZ_NORTH", 0, 20.0), _p("LZ_WEST", 0, 30.0)]
    zones = {"b-north": "LZ_NORTH", "b-west": "LZ_WEST", "b-aen": "LZ_AEN"}
    scenarios = scenarios_from_points(points, H0, zones)
    banks = tuple(
        replace(BankSnapshot(bank_id=b, max_discharge_kw={0: 10.0}), zone=z) for b, z in zones.items()
    )
    candidate = binary_candidate("c", 5.0, 50.0, (0,), tuple(zones))

    (withheld,) = withhold_unfit_series((candidate,), banks, scenarios, frozenset({"LZ_WEST"}))
    (untouched,) = withhold_unfit_series((candidate,), banks, scenarios, frozenset())

    assert withheld.eligible_bank_ids == ("b-north",)
    assert untouched.eligible_bank_ids == tuple(zones)


async def test_load_scenarios_warns_when_a_bank_zone_has_no_price_path(monkeypatch, caplog):
    """A bank in a zone forecast does not cover (LZ_AEN before it is in `[fleet].zones`) is priced at the
    fleet mean path, and the gate says so."""
    from opengrid.selector import db, gate

    async def _points(horizon_start, horizon_end):
        return [_p("LZ_NORTH", 0, 20.0)]

    async def _zones(bank_ids):
        return {"bank-000": "LZ_NORTH", "bank-aen": "LZ_AEN"}

    monkeypatch.setattr(gate.forecast, "scenarios", _points)
    monkeypatch.setattr(db, "load_bank_zones", _zones)

    (scenario,) = await gate.load_scenarios(H0, H0 + timedelta(hours=1), ("bank-000", "bank-aen"))

    assert scenario.price_at("bank-aen", 0) == 20.0
    assert any("LZ_AEN" in str(getattr(r, "zones", "")) for r in caplog.records)
