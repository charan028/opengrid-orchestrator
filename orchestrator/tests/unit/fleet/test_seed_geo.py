"""opengrid.fleet.seed.hub_lat_lon: deterministic per-hub geography (owner UI request, 2026-09-26,
#19). Cross-package parity with `ogsim.fleet.state.hub_lat_lon` is asserted directly here (both
modules are importable from this checkout's single venv, even though production code never imports
across the opengrid/ogsim boundary -- BUILD.md S1)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from opengrid.fleet.seed import ZONE_GEO_CENTERS, hub_lat_lon


def test_lat_lon_is_deterministic():
    assert hub_lat_lon(42, "LZ_NORTH") == hub_lat_lon(42, "LZ_NORTH")


def test_lat_lon_is_within_the_zone_cluster_radius():
    for zone, (center_lat, center_lon) in ZONE_GEO_CENTERS.items():
        for i in (0, 1, 999, 2000, 2999):
            lat, lon = hub_lat_lon(i, zone)
            assert abs(lat - center_lat) <= 0.35 / 2
            assert abs(lon - center_lon) <= 0.35 / 2


def test_unknown_zone_falls_back_to_texas_centroid_without_raising():
    lat, lon = hub_lat_lon(0, "LZ_NOT_A_REAL_ZONE")
    assert 25.0 <= lat <= 37.0  # loosely "somewhere in/near Texas"
    assert -107.0 <= lon <= -93.0


def test_different_hubs_get_different_points():
    points = {hub_lat_lon(i, "LZ_NORTH") for i in range(20)}
    assert len(points) > 1  # not all jittered to the same spot


def test_matches_ogsim_fleet_state_exactly():
    """Cross-package parity (BUILD.md S1 "share no code", proven directly since both packages are
    importable in this dev venv): identical formula, identical constants."""
    sims_src = str(Path(__file__).resolve().parents[4] / "integration-sims" / "src")
    if sims_src not in sys.path:
        sys.path.insert(0, sims_src)
    from ogsim.fleet.state import ZONE_GEO_CENTERS as OGSIM_CENTERS
    from ogsim.fleet.state import hub_lat_lon as ogsim_hub_lat_lon

    assert ZONE_GEO_CENTERS == OGSIM_CENTERS
    for zone in ZONE_GEO_CENTERS:
        for i in (0, 1, 500, 2000, 2999, 9999):
            assert hub_lat_lon(i, zone) == pytest.approx(ogsim_hub_lat_lon(i, zone))
