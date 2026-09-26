"""`wholesale_from_spp`: the settle wholesale basis is the zone SPP, never the contract price."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from opengrid.settle.pg_backend import wholesale_from_spp

_START = datetime(2026, 9, 26, 15, 0, tzinfo=UTC)


def test_exact_interval_spp_is_used_in_dollars_per_kwh():
    assert wholesale_from_spp({"zone": "LZ_HOUSTON", "ts": _START, "value": 16.47}, _START) == (
        Decimal("0.01647"),
        "SPP",
    )


def test_earlier_spp_is_flagged_prior():
    row = {"zone": "LZ_NORTH", "ts": _START - timedelta(minutes=15), "value": 18.25}
    assert wholesale_from_spp(row, _START) == (Decimal("0.01825"), "SPP_PRIOR")


def test_no_zone_or_no_spp_is_missing_and_zero():
    assert wholesale_from_spp(None, _START) == (Decimal("0"), "MISSING")
    assert wholesale_from_spp({"zone": "LZ_WEST", "ts": None, "value": None}, _START) == (
        Decimal("0"),
        "MISSING",
    )
    assert wholesale_from_spp({"zone": "LZ_WEST", "ts": _START, "value": 20.0}, None) == (
        Decimal("0"),
        "MISSING",
    )
