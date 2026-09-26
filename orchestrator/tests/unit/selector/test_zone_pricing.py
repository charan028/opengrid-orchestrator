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
