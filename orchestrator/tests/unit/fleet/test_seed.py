"""opengrid.fleet.seed: the og.hub/og.bank topology loader must reproduce ogsim.fleet.state's exact id
scheme (hub-NNNNN / bank-NNN) and be idempotent. No DB (BUILD.md S5) -- `seed_topology`'s SQL is
exercised against a minimal fake pool/cursor, matching `tests/unit/guardian/test_repo.py`'s pattern."""

from __future__ import annotations

from pathlib import Path

import pytest

from opengrid.fleet.seed import (
    SimFleetTopologyConfig,
    build_topology,
    load_sim_fleet_topology_config,
    resolve_sim_fleet_config_path,
    seed_topology,
)


class FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, dict]] = []

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, cursor: FakeCursor) -> None:
        self._conn = FakeConn(cursor)

    def connection(self):
        return self._conn


def test_build_topology_matches_ogsim_id_scheme():
    """Mirrors ogsim.fleet.state.build_fleet_state's exact formula: hub-{i:05d}, bank-{i%N:03d}."""
    config = SimFleetTopologyConfig(hub_count=5, bank_count=2, zones=("LZ_NORTH", "LZ_SOUTH"))
    topology = build_topology(config)

    assert [h.hub_id for h in topology.hubs] == [f"hub-{i:05d}" for i in range(5)]
    assert [h.bank_id for h in topology.hubs] == ["bank-000", "bank-001", "bank-000", "bank-001", "bank-000"]
    assert [h.zone for h in topology.hubs] == ["LZ_NORTH", "LZ_SOUTH", "LZ_NORTH", "LZ_SOUTH", "LZ_NORTH"]
    assert {b.bank_id for b in topology.banks} == {"bank-000", "bank-001"}


def test_build_topology_is_deterministic():
    config = SimFleetTopologyConfig(hub_count=200, bank_count=40)
    a = build_topology(config)
    b = build_topology(config)
    assert a == b


def test_build_topology_hub_physics_match_config():
    config = SimFleetTopologyConfig(hub_count=1, bank_count=1, e_kwh_default=13.5, reserve_frac_default=0.2)
    topology = build_topology(config)
    hub = topology.hubs[0]
    assert hub.e_kwh == pytest.approx(13.5)
    assert hub.r_kwh == pytest.approx(2.7)  # 13.5 * 0.2


def test_bank_zone_is_majority_of_its_hubs():
    """bank-000 gets hubs 0, 3, 6 (zones cycle LZ_NORTH/LZ_SOUTH): zones LZ_NORTH, LZ_SOUTH, LZ_NORTH
    -> LZ_NORTH is the majority (2 of 3)."""
    config = SimFleetTopologyConfig(hub_count=9, bank_count=3, zones=("LZ_NORTH", "LZ_SOUTH"))
    topology = build_topology(config)
    bank0 = next(b for b in topology.banks if b.bank_id == "bank-000")
    assert bank0.zone == "LZ_NORTH"


def test_resolve_sim_fleet_config_path_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OG_FLEET_SIM_CONFIG", "/some/override/fleet.yaml")
    assert resolve_sim_fleet_config_path() == Path("/some/override/fleet.yaml")


def test_resolve_sim_fleet_config_path_derives_from_og_config(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OG_FLEET_SIM_CONFIG", raising=False)
    monkeypatch.setenv("OG_CONFIG", "/opt/opengrid/current/orchestrator/config/orchestrator.toml")
    path = resolve_sim_fleet_config_path()
    assert path == Path("/opt/opengrid/current/integration-sims/config/fleet.yaml")


def test_load_sim_fleet_topology_config_falls_back_when_file_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OG_FLEET_SIM_CONFIG", "/does/not/exist.yaml")
    config = load_sim_fleet_topology_config()
    assert config == SimFleetTopologyConfig()  # matches ogsim.common.config.FleetConfig's own defaults


def test_load_sim_fleet_topology_config_reads_real_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yaml_path = tmp_path / "fleet.yaml"
    yaml_path.write_text("hub_count: 10\nbank_count: 3\nzones: [LZ_NORTH]\n", encoding="utf-8")
    monkeypatch.setenv("OG_FLEET_SIM_CONFIG", str(yaml_path))
    config = load_sim_fleet_topology_config()
    assert config.hub_count == 10
    assert config.bank_count == 3
    assert config.zones == ("LZ_NORTH",)


async def test_seed_topology_upserts_banks_then_hubs():
    config = SimFleetTopologyConfig(hub_count=3, bank_count=1, zones=("LZ_NORTH",))
    topology = build_topology(config)
    cursor = FakeCursor()
    pool = FakePool(cursor)

    result = await seed_topology(pool, topology)

    assert result.banks_upserted == 1
    assert result.hubs_upserted == 3
    bank_statements = [sql for sql, _ in cursor.executed if sql.strip().startswith("INSERT INTO og.bank")]
    hub_statements = [sql for sql, _ in cursor.executed if sql.strip().startswith("INSERT INTO og.hub")]
    assert len(bank_statements) == 1
    assert len(hub_statements) == 3
    assert all("ON CONFLICT" in sql for sql, _ in cursor.executed)
    assert pool._conn.committed is True
