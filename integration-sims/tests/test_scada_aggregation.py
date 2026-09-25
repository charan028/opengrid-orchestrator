"""Tests for ogsim.scada.aggregation -- per-bank kVA aggregation math."""

from __future__ import annotations

import pytest

from ogsim.scada.aggregation import BankTelemetryBuffer, bank_load_kw, kw_to_kva


def test_buffer_sums_member_hub_power() -> None:
    buffer = BankTelemetryBuffer()
    buffer.update("hub-1", 3.0)
    buffer.update("hub-2", -1.5)
    assert buffer.net_battery_kw() == pytest.approx(1.5)


def test_buffer_update_overwrites_latest_value() -> None:
    buffer = BankTelemetryBuffer()
    buffer.update("hub-1", 3.0)
    buffer.update("hub-1", -2.0)
    assert buffer.net_battery_kw() == pytest.approx(-2.0)


def test_drop_hub_removes_its_contribution() -> None:
    buffer = BankTelemetryBuffer()
    buffer.update("hub-1", 3.0)
    buffer.update("hub-2", 2.0)
    buffer.drop_hub("hub-1")
    assert buffer.net_battery_kw() == pytest.approx(2.0)


def test_bank_load_combines_background_and_battery() -> None:
    assert bank_load_kw(battery_net_kw=5.0, background_kw=100.0) == 105.0


def test_kw_to_kva_uses_assumed_power_factor() -> None:
    assert kw_to_kva(98.0, power_factor=0.98) == pytest.approx(100.0)


def test_kw_to_kva_takes_absolute_value() -> None:
    assert kw_to_kva(-98.0, power_factor=0.98) == pytest.approx(100.0)


def test_kw_to_kva_rejects_zero_power_factor() -> None:
    with pytest.raises(ValueError, match="power_factor"):
        kw_to_kva(10.0, power_factor=0.0)
