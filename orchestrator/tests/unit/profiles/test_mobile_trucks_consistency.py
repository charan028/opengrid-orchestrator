"""The owner's truck fleet (MOBILE_STORAGE, D-31, 2026-09-26): 2 Austin, 2 San Antonio, 4 Dallas trucks.

One truck set, four places that must agree 1:1 -- SERVICES' home-station registry (the source of truth), the
sims' fleet.yaml/scada.yaml `mobile_units:`, and dev/seed/mobile_trucks_seed.sql -- plus the readers that
key off the registry: `selector.gate.load_mobile_units` and the UI's `classify_asset`.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from opengrid.api.routers.fleet_search import classify_asset
from opengrid.selector import gate

_REPO_ROOT = Path(__file__).resolve().parents[4]
REGISTRY = _REPO_ROOT / "orchestrator" / "config" / "service_profiles" / "mobile_storage_home_stations.toml"
FLEET_YAML = _REPO_ROOT / "integration-sims" / "config" / "fleet.yaml"
SCADA_YAML = _REPO_ROOT / "integration-sims" / "config" / "scada.yaml"
SEED_SQL = _REPO_ROOT / "dev" / "seed" / "mobile_trucks_seed.sql"

TRUCKS = {
    "truck-aus-01",
    "truck-aus-02",
    "truck-sat-01",
    "truck-sat-02",
    "truck-dfw-01",
    "truck-dfw-02",
    "truck-dfw-03",
    "truck-dfw-04",
}
_SEED_ROW = re.compile(r"\('(truck-[a-z]+-\d+)',\s*'(LZ_[A-Z]+)',\s*(-?[\d.]+),\s*(-?[\d.]+)\)")


def _registry() -> dict[str, Any]:
    with REGISTRY.open("rb") as fh:
        return tomllib.load(fh)


def _truck_assignments() -> dict[str, dict[str, Any]]:
    return {str(a["hub_id"]): a for a in _registry()["assignment"] if a.get("hub_id") in TRUCKS}


def _stations() -> dict[str, dict[str, Any]]:
    return {str(s["home_station_id"]): s for s in _registry()["home_station"]}


def _sim_units(path: Path) -> dict[str, dict[str, Any]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {str(u["trailer_id"]): u for u in raw["mobile_units"] if u.get("simulate")}


def _seed_rows() -> dict[str, tuple[str, float, float]]:
    return {
        m[0]: (m[1], float(m[2]), float(m[3]))
        for m in _SEED_ROW.findall(SEED_SQL.read_text(encoding="utf-8"))
    }


def test_registry_has_eight_trucks_each_at_its_own_depot_by_region() -> None:
    assignments = _truck_assignments()
    assert set(assignments) == TRUCKS
    for hub_id, a in assignments.items():
        assert a["bank_id"] == f"bank-{hub_id}"  # a single-hub bank
    depots = [a["home_station_id"] for a in assignments.values()]
    assert len(set(depots)) == 8  # one depot per truck
    stations = _stations()
    for depot in depots:
        station = stations[depot]
        assert station["name"] and station["charger_kw"] == 500.0
        # Regulated territory sets its utility; a competitive zone leaves it empty.
        regulated = {"LZ_AEN": "AUSTIN_ENERGY", "LZ_CPS": "CPS_ENERGY"}
        assert station["utility_id"] == regulated.get(station["zone"], "")
    by_region = {r: sum(h.startswith(f"truck-{r}-") for h in assignments) for r in ("aus", "sat", "dfw")}
    assert by_region == {"aus": 2, "sat": 2, "dfw": 4}


@pytest.mark.parametrize("path", [FLEET_YAML, SCADA_YAML], ids=["fleet.yaml", "scada.yaml"])
def test_sim_mobile_units_match_the_registry(path: Path) -> None:
    units = _sim_units(path)
    assignments = _truck_assignments()
    stations = _stations()
    assert set(units) == TRUCKS
    for hub_id, unit in units.items():
        a = assignments[hub_id]
        assert unit["bank_id"] == a["bank_id"]
        assert unit["home_station_id"] == a["home_station_id"]
        assert unit["zone"] == stations[a["home_station_id"]]["zone"]
        assert unit["p_kw"] == 500.0


def test_fleet_yaml_trucks_are_parked_at_their_home_station() -> None:
    stations = _stations()
    for hub_id, unit in _sim_units(FLEET_YAML).items():
        station = stations[_truck_assignments()[hub_id]["home_station_id"]]
        assert (unit["lat"], unit["lon"]) == (station["lat"], station["lon"])
        assert unit["at_home"] is True
        assert (unit["e_kwh"], unit["reserve_frac"]) == (1000.0, 0.20)


def test_seed_matches_the_registry() -> None:
    rows = _seed_rows()
    stations = _stations()
    assert set(rows) == TRUCKS
    for hub_id, (zone, lat, lon) in rows.items():
        station = stations[_truck_assignments()[hub_id]["home_station_id"]]
        assert (zone, lat, lon) == (station["zone"], station["lat"], station["lon"])
    sql = SEED_SQL.read_text(encoding="utf-8")
    assert "SET units = 1" in sql  # 0032's trigger would make a 1 MWh hub dual-unit
    assert "ON CONFLICT" in sql
    assert "'MOBILE_STORAGE'" in sql  # og.asset rows: G-02 nameplate (migration 0044)


def test_load_mobile_units_sees_the_eight_trucks_at_their_home_zones(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OG_CONFIG", raising=False)
    mobile = gate.load_mobile_units()
    trucks = {k: v for k, v in mobile.items() if k.startswith("bank-truck-")}
    assert len(trucks) == 8
    assert trucks["bank-truck-aus-02"] == "LZ_AEN"
    assert trucks["bank-truck-sat-01"] == "LZ_CPS"
    assert trucks["bank-truck-dfw-04"] == "LZ_NORTH"
    assert mobile["trailer-mb-01"] == "LZ_AEN"  # the existing trailer still works
    sites = gate.load_mobile_home_station_sites()
    assert sites["truck-dfw-01"] == sites["bank-truck-dfw-01"] == (32.8385, -96.9730)


def test_classify_asset_marks_every_truck_mobile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OG_CONFIG", raising=False)
    mobile = set(gate.load_mobile_units())
    for hub_id in TRUCKS:
        assert classify_asset(hub_id, f"bank-{hub_id}", mobile=mobile, substations=set()) == "MOBILE"
    assert classify_asset("hub-00001", "bank-001", mobile=mobile, substations=set()) == "HOME"
