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

# Expected dual-unit hub ids for hub_count=2000, dual_unit_share=0.2 (400 of 2,000 hubs). This
# literal list is duplicated verbatim in integration-sims/tests/test_fleet_dual_unit.py -- it is
# NOT computed via the dual-unit formula here, so the two tests can't both be fooled by the same
# formula bug, and opengrid/ogsim still share no code (BUILD.md S1).
EXPECTED_DUAL_UNIT_HUB_IDS: tuple[str, ...] = (
    "hub-00004",
    "hub-00009",
    "hub-00014",
    "hub-00019",
    "hub-00024",
    "hub-00029",
    "hub-00034",
    "hub-00039",
    "hub-00044",
    "hub-00049",
    "hub-00054",
    "hub-00059",
    "hub-00064",
    "hub-00069",
    "hub-00074",
    "hub-00079",
    "hub-00084",
    "hub-00089",
    "hub-00094",
    "hub-00099",
    "hub-00104",
    "hub-00109",
    "hub-00114",
    "hub-00119",
    "hub-00124",
    "hub-00129",
    "hub-00134",
    "hub-00139",
    "hub-00144",
    "hub-00149",
    "hub-00154",
    "hub-00159",
    "hub-00164",
    "hub-00169",
    "hub-00174",
    "hub-00179",
    "hub-00184",
    "hub-00189",
    "hub-00194",
    "hub-00199",
    "hub-00204",
    "hub-00209",
    "hub-00214",
    "hub-00219",
    "hub-00224",
    "hub-00229",
    "hub-00234",
    "hub-00239",
    "hub-00244",
    "hub-00249",
    "hub-00254",
    "hub-00259",
    "hub-00264",
    "hub-00269",
    "hub-00274",
    "hub-00279",
    "hub-00284",
    "hub-00289",
    "hub-00294",
    "hub-00299",
    "hub-00304",
    "hub-00309",
    "hub-00314",
    "hub-00319",
    "hub-00324",
    "hub-00329",
    "hub-00334",
    "hub-00339",
    "hub-00344",
    "hub-00349",
    "hub-00354",
    "hub-00359",
    "hub-00364",
    "hub-00369",
    "hub-00374",
    "hub-00379",
    "hub-00384",
    "hub-00389",
    "hub-00394",
    "hub-00399",
    "hub-00404",
    "hub-00409",
    "hub-00414",
    "hub-00419",
    "hub-00424",
    "hub-00429",
    "hub-00434",
    "hub-00439",
    "hub-00444",
    "hub-00449",
    "hub-00454",
    "hub-00459",
    "hub-00464",
    "hub-00469",
    "hub-00474",
    "hub-00479",
    "hub-00484",
    "hub-00489",
    "hub-00494",
    "hub-00499",
    "hub-00504",
    "hub-00509",
    "hub-00514",
    "hub-00519",
    "hub-00524",
    "hub-00529",
    "hub-00534",
    "hub-00539",
    "hub-00544",
    "hub-00549",
    "hub-00554",
    "hub-00559",
    "hub-00564",
    "hub-00569",
    "hub-00574",
    "hub-00579",
    "hub-00584",
    "hub-00589",
    "hub-00594",
    "hub-00599",
    "hub-00604",
    "hub-00609",
    "hub-00614",
    "hub-00619",
    "hub-00624",
    "hub-00629",
    "hub-00634",
    "hub-00639",
    "hub-00644",
    "hub-00649",
    "hub-00654",
    "hub-00659",
    "hub-00664",
    "hub-00669",
    "hub-00674",
    "hub-00679",
    "hub-00684",
    "hub-00689",
    "hub-00694",
    "hub-00699",
    "hub-00704",
    "hub-00709",
    "hub-00714",
    "hub-00719",
    "hub-00724",
    "hub-00729",
    "hub-00734",
    "hub-00739",
    "hub-00744",
    "hub-00749",
    "hub-00754",
    "hub-00759",
    "hub-00764",
    "hub-00769",
    "hub-00774",
    "hub-00779",
    "hub-00784",
    "hub-00789",
    "hub-00794",
    "hub-00799",
    "hub-00804",
    "hub-00809",
    "hub-00814",
    "hub-00819",
    "hub-00824",
    "hub-00829",
    "hub-00834",
    "hub-00839",
    "hub-00844",
    "hub-00849",
    "hub-00854",
    "hub-00859",
    "hub-00864",
    "hub-00869",
    "hub-00874",
    "hub-00879",
    "hub-00884",
    "hub-00889",
    "hub-00894",
    "hub-00899",
    "hub-00904",
    "hub-00909",
    "hub-00914",
    "hub-00919",
    "hub-00924",
    "hub-00929",
    "hub-00934",
    "hub-00939",
    "hub-00944",
    "hub-00949",
    "hub-00954",
    "hub-00959",
    "hub-00964",
    "hub-00969",
    "hub-00974",
    "hub-00979",
    "hub-00984",
    "hub-00989",
    "hub-00994",
    "hub-00999",
    "hub-01004",
    "hub-01009",
    "hub-01014",
    "hub-01019",
    "hub-01024",
    "hub-01029",
    "hub-01034",
    "hub-01039",
    "hub-01044",
    "hub-01049",
    "hub-01054",
    "hub-01059",
    "hub-01064",
    "hub-01069",
    "hub-01074",
    "hub-01079",
    "hub-01084",
    "hub-01089",
    "hub-01094",
    "hub-01099",
    "hub-01104",
    "hub-01109",
    "hub-01114",
    "hub-01119",
    "hub-01124",
    "hub-01129",
    "hub-01134",
    "hub-01139",
    "hub-01144",
    "hub-01149",
    "hub-01154",
    "hub-01159",
    "hub-01164",
    "hub-01169",
    "hub-01174",
    "hub-01179",
    "hub-01184",
    "hub-01189",
    "hub-01194",
    "hub-01199",
    "hub-01204",
    "hub-01209",
    "hub-01214",
    "hub-01219",
    "hub-01224",
    "hub-01229",
    "hub-01234",
    "hub-01239",
    "hub-01244",
    "hub-01249",
    "hub-01254",
    "hub-01259",
    "hub-01264",
    "hub-01269",
    "hub-01274",
    "hub-01279",
    "hub-01284",
    "hub-01289",
    "hub-01294",
    "hub-01299",
    "hub-01304",
    "hub-01309",
    "hub-01314",
    "hub-01319",
    "hub-01324",
    "hub-01329",
    "hub-01334",
    "hub-01339",
    "hub-01344",
    "hub-01349",
    "hub-01354",
    "hub-01359",
    "hub-01364",
    "hub-01369",
    "hub-01374",
    "hub-01379",
    "hub-01384",
    "hub-01389",
    "hub-01394",
    "hub-01399",
    "hub-01404",
    "hub-01409",
    "hub-01414",
    "hub-01419",
    "hub-01424",
    "hub-01429",
    "hub-01434",
    "hub-01439",
    "hub-01444",
    "hub-01449",
    "hub-01454",
    "hub-01459",
    "hub-01464",
    "hub-01469",
    "hub-01474",
    "hub-01479",
    "hub-01484",
    "hub-01489",
    "hub-01494",
    "hub-01499",
    "hub-01504",
    "hub-01509",
    "hub-01514",
    "hub-01519",
    "hub-01524",
    "hub-01529",
    "hub-01534",
    "hub-01539",
    "hub-01544",
    "hub-01549",
    "hub-01554",
    "hub-01559",
    "hub-01564",
    "hub-01569",
    "hub-01574",
    "hub-01579",
    "hub-01584",
    "hub-01589",
    "hub-01594",
    "hub-01599",
    "hub-01604",
    "hub-01609",
    "hub-01614",
    "hub-01619",
    "hub-01624",
    "hub-01629",
    "hub-01634",
    "hub-01639",
    "hub-01644",
    "hub-01649",
    "hub-01654",
    "hub-01659",
    "hub-01664",
    "hub-01669",
    "hub-01674",
    "hub-01679",
    "hub-01684",
    "hub-01689",
    "hub-01694",
    "hub-01699",
    "hub-01704",
    "hub-01709",
    "hub-01714",
    "hub-01719",
    "hub-01724",
    "hub-01729",
    "hub-01734",
    "hub-01739",
    "hub-01744",
    "hub-01749",
    "hub-01754",
    "hub-01759",
    "hub-01764",
    "hub-01769",
    "hub-01774",
    "hub-01779",
    "hub-01784",
    "hub-01789",
    "hub-01794",
    "hub-01799",
    "hub-01804",
    "hub-01809",
    "hub-01814",
    "hub-01819",
    "hub-01824",
    "hub-01829",
    "hub-01834",
    "hub-01839",
    "hub-01844",
    "hub-01849",
    "hub-01854",
    "hub-01859",
    "hub-01864",
    "hub-01869",
    "hub-01874",
    "hub-01879",
    "hub-01884",
    "hub-01889",
    "hub-01894",
    "hub-01899",
    "hub-01904",
    "hub-01909",
    "hub-01914",
    "hub-01919",
    "hub-01924",
    "hub-01929",
    "hub-01934",
    "hub-01939",
    "hub-01944",
    "hub-01949",
    "hub-01954",
    "hub-01959",
    "hub-01964",
    "hub-01969",
    "hub-01974",
    "hub-01979",
    "hub-01984",
    "hub-01989",
    "hub-01994",
    "hub-01999",
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


def test_dual_unit_count_is_exactly_400_of_2000():
    """20% of 2,000 hubs (the MVP-S fleet scale) are dual-unit, per the deterministic rule in
    `build_topology`'s docstring."""
    config = SimFleetTopologyConfig()  # hub_count=2000, dual_unit_share=0.2 defaults
    topology = build_topology(config)
    dual_unit_hubs = [h for h in topology.hubs if h.e_kwh == pytest.approx(config.e_kwh_dual_unit)]
    assert len(dual_unit_hubs) == 400


def test_dual_unit_hub_ids_match_expected_fixture():
    """The set of dual-unit hub ids must be identical to `ogsim.fleet.state`'s (see
    `EXPECTED_DUAL_UNIT_HUB_IDS`'s docstring) -- this is what keeps hub-level dual-unit physics
    lined up between the orchestrator's seeded topology and the simulator's telemetry."""
    config = SimFleetTopologyConfig()
    topology = build_topology(config)
    dual_unit_ids = {h.hub_id for h in topology.hubs if h.e_kwh == pytest.approx(config.e_kwh_dual_unit)}
    assert dual_unit_ids == set(EXPECTED_DUAL_UNIT_HUB_IDS)


def test_dual_unit_hub_power_and_reserve_match_config():
    config = SimFleetTopologyConfig()
    topology = build_topology(config)
    dual_hub = next(h for h in topology.hubs if h.hub_id == "hub-00004")
    assert dual_hub.e_kwh == pytest.approx(config.e_kwh_dual_unit)
    assert dual_hub.p_kw == pytest.approx(config.p_kw_dual_unit)
    assert dual_hub.r_kwh == pytest.approx(config.e_kwh_dual_unit * config.reserve_frac_default)

    single_hub = next(h for h in topology.hubs if h.hub_id == "hub-00000")
    assert single_hub.e_kwh == pytest.approx(config.e_kwh_default)
    assert single_hub.p_kw == pytest.approx(config.p_kw_default)


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
