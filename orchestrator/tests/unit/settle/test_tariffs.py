"""M1 delivery charge (09-optimizer-dispatcher-update.md D5): tariff loading/resolution and the
charge formula, against the real `orchestrator/config/tdsp_tariffs.toml`."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from opengrid.settle.models import ZoneChargeEnergy
from opengrid.settle.tariffs import (
    TdspTariff,
    grid_charged_kwh_for_delivery,
    load_tdsp_tariffs,
    m1_delivery_charge,
    resolve_tariff,
    resolve_tdsp_tariffs_path,
    tdsp_for_zone,
)

_TARIFFS_PATH = Path(__file__).resolve().parents[4] / "orchestrator" / "config" / "tdsp_tariffs.toml"


def test_grid_share_is_the_grid_fraction_of_charging_and_full_when_nothing_charged():
    assert ZoneChargeEnergy(Decimal("400"), Decimal("100")).grid_share == Decimal("0.25")
    assert ZoneChargeEnergy(Decimal("0"), Decimal("0")).grid_share == Decimal("1")  # owner: assume full M1
    assert ZoneChargeEnergy(Decimal("10"), Decimal("12")).grid_share == Decimal("1")  # clamped


def test_grid_charged_kwh_for_delivery_is_the_energy_charged_for_it_times_the_grid_share():
    kwh = grid_charged_kwh_for_delivery(
        Decimal("9"), eta_c=Decimal("0.9487"), eta_d=Decimal("0.9487"), grid_share=Decimal("0.5")
    )
    assert kwh == Decimal("9") / (Decimal("0.9487") * Decimal("0.9487")) * Decimal("0.5")
    assert grid_charged_kwh_for_delivery(
        Decimal("0"), eta_c=Decimal("0.95"), eta_d=Decimal("0.95"), grid_share=Decimal("1")
    ) == Decimal("0")
    assert grid_charged_kwh_for_delivery(
        Decimal("5"), eta_c=Decimal("0"), eta_d=Decimal("0.95"), grid_share=Decimal("1")
    ) == Decimal("0")


def test_regulated_zones_have_no_tdsp_in_the_real_tariff_file(loaded_tariffs):
    """Owner: FREE-market zones only; Austin Energy / CPS Energy (regulated) pay no M1."""
    _tariffs, zone_default = loaded_tariffs
    assert tdsp_for_zone(zone_default, "LZ_AEN") is None
    assert tdsp_for_zone(zone_default, "LZ_CPS") is None


@pytest.fixture(scope="module")
def loaded_tariffs() -> tuple[list[TdspTariff], dict[str, str]]:
    return load_tdsp_tariffs(_TARIFFS_PATH)


def test_oncor_tariff_is_6_0295_cents_per_kwh(loaded_tariffs):
    tariffs, _zone_default = loaded_tariffs
    tariff = resolve_tariff(tariffs, "ONCOR", date(2026, 9, 26))
    assert tariff is not None
    assert tariff.volumetric_usd_per_kwh == Decimal("0.060295")


def test_m1_charge_oncor_hand_computed():
    """100 kWh drawn from the grid at Oncor's 6.0295 cents/kWh = $6.0295."""
    tariff = TdspTariff(
        tdsp="ONCOR",
        effective_from=date(2026, 9, 1),
        volumetric_usd_per_kwh=Decimal("0.060295"),
        load_zones=("LZ_NORTH", "LZ_WEST"),
    )
    assert m1_delivery_charge(Decimal("100"), tariff) == Decimal("6.0295")


def test_zone_default_tdsp_resolves_north_to_oncor(loaded_tariffs):
    _tariffs, zone_default = loaded_tariffs
    assert tdsp_for_zone(zone_default, "LZ_NORTH") == "ONCOR"


def test_no_tariff_for_a_regulated_or_unmapped_zone_charges_nothing(loaded_tariffs):
    """Austin Energy/CPS Energy territory has no entry in [zone_default_tdsp] (09 D5: their delivery
    cost is inside the utility's own contract terms, never a TDSP line) -- and any other unmapped
    zone must resolve the same way: no tariff, no charge."""
    _tariffs, zone_default = loaded_tariffs
    tdsp = tdsp_for_zone(zone_default, "LZ_AUSTIN")
    assert tdsp is None
    tariff = resolve_tariff(_tariffs, tdsp, date(2026, 9, 26))
    assert tariff is None
    assert m1_delivery_charge(Decimal("100"), tariff) == Decimal("0")


def test_resolve_tariff_picks_the_latest_effective_block_not_a_future_one():
    tariffs = [
        TdspTariff(
            tdsp="ONCOR",
            effective_from=date(2026, 3, 1),
            volumetric_usd_per_kwh=Decimal("0.05"),
            load_zones=("LZ_NORTH",),
        ),
        TdspTariff(
            tdsp="ONCOR",
            effective_from=date(2026, 9, 1),
            volumetric_usd_per_kwh=Decimal("0.060295"),
            load_zones=("LZ_NORTH",),
        ),
        TdspTariff(
            tdsp="ONCOR",
            effective_from=date(2027, 3, 1),
            volumetric_usd_per_kwh=Decimal("0.07"),
            load_zones=("LZ_NORTH",),
        ),
    ]
    tariff = resolve_tariff(tariffs, "ONCOR", date(2026, 12, 1))
    assert tariff is not None
    assert tariff.volumetric_usd_per_kwh == Decimal("0.060295")


def test_resolve_tariff_none_when_tdsp_is_none():
    assert resolve_tariff([], None, date(2026, 9, 26)) is None


def test_resolve_tdsp_tariffs_path_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OG_TDSP_TARIFFS_PATH", "/some/override/tdsp_tariffs.toml")
    assert resolve_tdsp_tariffs_path() == Path("/some/override/tdsp_tariffs.toml")


def test_resolve_tdsp_tariffs_path_derives_from_og_config(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OG_TDSP_TARIFFS_PATH", raising=False)
    monkeypatch.setenv("OG_CONFIG", "/opt/opengrid/current/orchestrator/config/orchestrator.toml")
    path = resolve_tdsp_tariffs_path()
    assert path == Path("/opt/opengrid/current/orchestrator/config/tdsp_tariffs.toml")
