"""02a S7.1 M&V baselines per service."""

from __future__ import annotations

from decimal import Decimal

from opengrid.settle.baselines import METER_SOURCE_BY_SERVICE, compute_baseline_kwh, has_baseline


def test_home_has_no_baseline():
    assert has_baseline("HOME") is False
    assert compute_baseline_kwh("HOME", Decimal("5"), Decimal("0.25")) is None


def test_dist_deferral_baseline_is_committed_kw_times_duration():
    """Hand computation: 10 kW committed for 0.25 h (a 15-min interval) -> K_{o,j} = 2.5 kWh."""
    baseline = compute_baseline_kwh("DIST_DEFERRAL", Decimal("10"), Decimal("0.25"))
    assert baseline == Decimal("2.5")


def test_every_non_home_service_has_a_meter_source():
    for service in ("ERCOT_ENERGY", "ERCOT_AS", "DIST_DEFERRAL", "PARTNER_CAPACITY"):
        assert service in METER_SOURCE_BY_SERVICE
        assert has_baseline(service) is True
