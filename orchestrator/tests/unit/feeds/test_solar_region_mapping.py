"""The shipped `[feeds.ercot.solar_share_zones]` (D-28 solar share, WP-L part 3) parses with the real
loader and covers every NP6-345-CD weather zone and every NP4-745-CD solar region.
Rationale: docs/orchestrator/07-delivery/integrations/solar-region-mapping.md."""

from __future__ import annotations

from pathlib import Path

import pytest

from opengrid.feeds.normalize import _LOAD_ZONE_COLUMNS, SOLAR_REGIONS
from opengrid.feeds.solar_share import SYSTEM_ZONE, solar_regions_for_zone, zone_regions_from_config
from opengrid.forecast.service import DEFAULT_WEATHER_ZONES_BY_LOAD_ZONE
from opengrid.platform.config import load_config

OG_CONFIG = Path(__file__).resolve().parents[3] / "config" / "orchestrator.toml"


@pytest.fixture
def zone_regions(monkeypatch: pytest.MonkeyPatch) -> dict[str, tuple[str, ...]]:
    monkeypatch.delenv("OG_DB", raising=False)
    monkeypatch.delenv("OG_MQTT_ROOT", raising=False)
    return zone_regions_from_config(load_config(OG_CONFIG))


def test_every_weather_zone_is_mapped(zone_regions: dict[str, tuple[str, ...]]) -> None:
    weather_zones = {z for z in _LOAD_ZONE_COLUMNS if z != SYSTEM_ZONE}
    assert set(zone_regions) == weather_zones


def test_every_solar_region_is_used(zone_regions: dict[str, tuple[str, ...]]) -> None:
    used = {r for regions in zone_regions.values() for r in regions}
    assert used == set(SOLAR_REGIONS)


def test_every_load_zone_reaches_solar_regions(zone_regions: dict[str, tuple[str, ...]]) -> None:
    for load_zone, weather_zones in DEFAULT_WEATHER_ZONES_BY_LOAD_ZONE.items():
        for wz in weather_zones:
            assert solar_regions_for_zone(wz, zone_regions), f"{load_zone} -> {wz} has no solar region"
    # Spot checks against ERCOT's county table: Travis (Austin) is CenterEast, Bexar is SouthEast.
    assert zone_regions["southC"] == ("CenterEast", "SouthEast")
    assert zone_regions["coast"] == ("FarEast",)
