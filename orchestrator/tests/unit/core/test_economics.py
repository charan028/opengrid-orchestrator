"""Tests for `opengrid.core.economics.wear_cost` (Frank #7, `09-optimizer-dispatcher-update.md` D8):
the ONE function both the selector and settle use for wear/degradation cost."""

from __future__ import annotations

from decimal import Decimal

import pytest

from opengrid.core.economics import wear_cost


def test_wear_cost_is_kwh_times_rate():
    assert wear_cost(Decimal("10"), Decimal("0.03")) == Decimal("0.30")


def test_wear_cost_zero_kwh_is_zero():
    assert wear_cost(Decimal("0"), Decimal("0.03")) == Decimal("0")


def test_wear_cost_uses_the_callers_own_rate_per_asset_class():
    """Homes ($0.03/kWh, A-DE-16) and substation assets ($0.015/kWh, OQ-15) share the same formula --
    the asset-class distinction lives entirely in which rate the caller passes in."""
    home = wear_cost(Decimal("100"), Decimal("0.03"))
    substation = wear_cost(Decimal("100"), Decimal("0.015"))
    assert home == Decimal("3.00")
    assert substation == Decimal("1.50")


def test_wear_cost_rejects_a_negative_kwh():
    """Wear is never charged on charging (09 D8) -- a negative (charging) quantity is a caller bug,
    not a value this function should silently price."""
    with pytest.raises(ValueError, match="never charged on charging"):
        wear_cost(Decimal("-5"), Decimal("0.03"))
