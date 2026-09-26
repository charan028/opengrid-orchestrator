"""opengrid.fleet.seed: the og.hub/og.bank topology loader must reproduce ogsim.fleet.state's exact id
scheme (hub-NNNNN / bank-NNN) and be idempotent. No DB (BUILD.md S5) -- `seed_topology`'s SQL is
exercised against a minimal fake pool/cursor, matching `tests/unit/guardian/test_repo.py`'s pattern."""

from __future__ import annotations

from pathlib import Path

import pytest

from opengrid.fleet.seed import (
    BANKS_PER_FEEDER_DEFAULT,
    SimFleetTopologyConfig,
    build_topology,
    feeder_id_for,
    load_sim_fleet_topology_config,
    resolve_sim_fleet_config_path,
    seed_topology,
)

# Expected dual-unit hub ids for hub_count=2000, dual_unit_share=0.2 (400 of 2,000 hubs). This
# literal list is duplicated verbatim in integration-sims/tests/test_fleet_dual_unit.py -- it is
# NOT computed via the dual-unit formula here, so the two tests can't both be fooled by the same
# formula bug, and opengrid/ogsim still share no code (BUILD.md S1).
EXPECTED_DUAL_UNIT_HUB_IDS: tuple[str, ...] = (
    "hub-00160",
    "hub-00161",
    "hub-00162",
    "hub-00163",
    "hub-00164",
    "hub-00165",
    "hub-00166",
    "hub-00167",
    "hub-00168",
    "hub-00169",
    "hub-00170",
    "hub-00171",
    "hub-00172",
    "hub-00173",
    "hub-00174",
    "hub-00175",
    "hub-00176",
    "hub-00177",
    "hub-00178",
    "hub-00179",
    "hub-00180",
    "hub-00181",
    "hub-00182",
    "hub-00183",
    "hub-00184",
    "hub-00185",
    "hub-00186",
    "hub-00187",
    "hub-00188",
    "hub-00189",
    "hub-00190",
    "hub-00191",
    "hub-00192",
    "hub-00193",
    "hub-00194",
    "hub-00195",
    "hub-00196",
    "hub-00197",
    "hub-00198",
    "hub-00199",
    "hub-00360",
    "hub-00361",
    "hub-00362",
    "hub-00363",
    "hub-00364",
    "hub-00365",
    "hub-00366",
    "hub-00367",
    "hub-00368",
    "hub-00369",
    "hub-00370",
    "hub-00371",
    "hub-00372",
    "hub-00373",
    "hub-00374",
    "hub-00375",
    "hub-00376",
    "hub-00377",
    "hub-00378",
    "hub-00379",
    "hub-00380",
    "hub-00381",
    "hub-00382",
    "hub-00383",
    "hub-00384",
    "hub-00385",
    "hub-00386",
    "hub-00387",
    "hub-00388",
    "hub-00389",
    "hub-00390",
    "hub-00391",
    "hub-00392",
    "hub-00393",
    "hub-00394",
    "hub-00395",
    "hub-00396",
    "hub-00397",
    "hub-00398",
    "hub-00399",
    "hub-00560",
    "hub-00561",
    "hub-00562",
    "hub-00563",
    "hub-00564",
    "hub-00565",
    "hub-00566",
    "hub-00567",
    "hub-00568",
    "hub-00569",
    "hub-00570",
    "hub-00571",
    "hub-00572",
    "hub-00573",
    "hub-00574",
    "hub-00575",
    "hub-00576",
    "hub-00577",
    "hub-00578",
    "hub-00579",
    "hub-00580",
    "hub-00581",
    "hub-00582",
    "hub-00583",
    "hub-00584",
    "hub-00585",
    "hub-00586",
    "hub-00587",
    "hub-00588",
    "hub-00589",
    "hub-00590",
    "hub-00591",
    "hub-00592",
    "hub-00593",
    "hub-00594",
    "hub-00595",
    "hub-00596",
    "hub-00597",
    "hub-00598",
    "hub-00599",
    "hub-00760",
    "hub-00761",
    "hub-00762",
    "hub-00763",
    "hub-00764",
    "hub-00765",
    "hub-00766",
    "hub-00767",
    "hub-00768",
    "hub-00769",
    "hub-00770",
    "hub-00771",
    "hub-00772",
    "hub-00773",
    "hub-00774",
    "hub-00775",
    "hub-00776",
    "hub-00777",
    "hub-00778",
    "hub-00779",
    "hub-00780",
    "hub-00781",
    "hub-00782",
    "hub-00783",
    "hub-00784",
    "hub-00785",
    "hub-00786",
    "hub-00787",
    "hub-00788",
    "hub-00789",
    "hub-00790",
    "hub-00791",
    "hub-00792",
    "hub-00793",
    "hub-00794",
    "hub-00795",
    "hub-00796",
    "hub-00797",
    "hub-00798",
    "hub-00799",
    "hub-00960",
    "hub-00961",
    "hub-00962",
    "hub-00963",
    "hub-00964",
    "hub-00965",
    "hub-00966",
    "hub-00967",
    "hub-00968",
    "hub-00969",
    "hub-00970",
    "hub-00971",
    "hub-00972",
    "hub-00973",
    "hub-00974",
    "hub-00975",
    "hub-00976",
    "hub-00977",
    "hub-00978",
    "hub-00979",
    "hub-00980",
    "hub-00981",
    "hub-00982",
    "hub-00983",
    "hub-00984",
    "hub-00985",
    "hub-00986",
    "hub-00987",
    "hub-00988",
    "hub-00989",
    "hub-00990",
    "hub-00991",
    "hub-00992",
    "hub-00993",
    "hub-00994",
    "hub-00995",
    "hub-00996",
    "hub-00997",
    "hub-00998",
    "hub-00999",
    "hub-01160",
    "hub-01161",
    "hub-01162",
    "hub-01163",
    "hub-01164",
    "hub-01165",
    "hub-01166",
    "hub-01167",
    "hub-01168",
    "hub-01169",
    "hub-01170",
    "hub-01171",
    "hub-01172",
    "hub-01173",
    "hub-01174",
    "hub-01175",
    "hub-01176",
    "hub-01177",
    "hub-01178",
    "hub-01179",
    "hub-01180",
    "hub-01181",
    "hub-01182",
    "hub-01183",
    "hub-01184",
    "hub-01185",
    "hub-01186",
    "hub-01187",
    "hub-01188",
    "hub-01189",
    "hub-01190",
    "hub-01191",
    "hub-01192",
    "hub-01193",
    "hub-01194",
    "hub-01195",
    "hub-01196",
    "hub-01197",
    "hub-01198",
    "hub-01199",
    "hub-01360",
    "hub-01361",
    "hub-01362",
    "hub-01363",
    "hub-01364",
    "hub-01365",
    "hub-01366",
    "hub-01367",
    "hub-01368",
    "hub-01369",
    "hub-01370",
    "hub-01371",
    "hub-01372",
    "hub-01373",
    "hub-01374",
    "hub-01375",
    "hub-01376",
    "hub-01377",
    "hub-01378",
    "hub-01379",
    "hub-01380",
    "hub-01381",
    "hub-01382",
    "hub-01383",
    "hub-01384",
    "hub-01385",
    "hub-01386",
    "hub-01387",
    "hub-01388",
    "hub-01389",
    "hub-01390",
    "hub-01391",
    "hub-01392",
    "hub-01393",
    "hub-01394",
    "hub-01395",
    "hub-01396",
    "hub-01397",
    "hub-01398",
    "hub-01399",
    "hub-01560",
    "hub-01561",
    "hub-01562",
    "hub-01563",
    "hub-01564",
    "hub-01565",
    "hub-01566",
    "hub-01567",
    "hub-01568",
    "hub-01569",
    "hub-01570",
    "hub-01571",
    "hub-01572",
    "hub-01573",
    "hub-01574",
    "hub-01575",
    "hub-01576",
    "hub-01577",
    "hub-01578",
    "hub-01579",
    "hub-01580",
    "hub-01581",
    "hub-01582",
    "hub-01583",
    "hub-01584",
    "hub-01585",
    "hub-01586",
    "hub-01587",
    "hub-01588",
    "hub-01589",
    "hub-01590",
    "hub-01591",
    "hub-01592",
    "hub-01593",
    "hub-01594",
    "hub-01595",
    "hub-01596",
    "hub-01597",
    "hub-01598",
    "hub-01599",
    "hub-01760",
    "hub-01761",
    "hub-01762",
    "hub-01763",
    "hub-01764",
    "hub-01765",
    "hub-01766",
    "hub-01767",
    "hub-01768",
    "hub-01769",
    "hub-01770",
    "hub-01771",
    "hub-01772",
    "hub-01773",
    "hub-01774",
    "hub-01775",
    "hub-01776",
    "hub-01777",
    "hub-01778",
    "hub-01779",
    "hub-01780",
    "hub-01781",
    "hub-01782",
    "hub-01783",
    "hub-01784",
    "hub-01785",
    "hub-01786",
    "hub-01787",
    "hub-01788",
    "hub-01789",
    "hub-01790",
    "hub-01791",
    "hub-01792",
    "hub-01793",
    "hub-01794",
    "hub-01795",
    "hub-01796",
    "hub-01797",
    "hub-01798",
    "hub-01799",
    "hub-01960",
    "hub-01961",
    "hub-01962",
    "hub-01963",
    "hub-01964",
    "hub-01965",
    "hub-01966",
    "hub-01967",
    "hub-01968",
    "hub-01969",
    "hub-01970",
    "hub-01971",
    "hub-01972",
    "hub-01973",
    "hub-01974",
    "hub-01975",
    "hub-01976",
    "hub-01977",
    "hub-01978",
    "hub-01979",
    "hub-01980",
    "hub-01981",
    "hub-01982",
    "hub-01983",
    "hub-01984",
    "hub-01985",
    "hub-01986",
    "hub-01987",
    "hub-01988",
    "hub-01989",
    "hub-01990",
    "hub-01991",
    "hub-01992",
    "hub-01993",
    "hub-01994",
    "hub-01995",
    "hub-01996",
    "hub-01997",
    "hub-01998",
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
    """Mirrors ogsim.fleet.state.build_fleet_state's exact formula: hub-{i:05d}, bank-{i%N:03d}
    round-robin -- banks are feeder segments and must stay single-zone (the dual-unit rule is offset
    per-bank instead, see `_is_dual_unit`'s docstring)."""
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
    # hub-00160 is dual-unit under the per-bank-offset rule: k = 160 // 40 = 4, and k % 5 == 4 selects
    # every 5th rank *within* a bank (see EXPECTED_DUAL_UNIT_HUB_IDS/`_is_dual_unit`'s docstring).
    dual_hub = next(h for h in topology.hubs if h.hub_id == "hub-00160")
    assert dual_hub.e_kwh == pytest.approx(config.e_kwh_dual_unit)
    assert dual_hub.p_kw == pytest.approx(config.p_kw_dual_unit)
    assert dual_hub.r_kwh == pytest.approx(config.e_kwh_dual_unit * config.reserve_frac_default)

    single_hub = next(h for h in topology.hubs if h.hub_id == "hub-00000")
    assert single_hub.e_kwh == pytest.approx(config.e_kwh_default)
    assert single_hub.p_kw == pytest.approx(config.p_kw_default)


def test_dual_unit_homes_spread_10_per_bank_not_clustered_in_8_banks():
    """Bug regression: indexing the dual-unit rule by the hub's *fleet-wide* index (every 5th hub
    overall) landed every dual-unit hub in a bank number congruent to 4 mod 5 (since `i % bank_count`
    bank assignment ties a hub's bank to the same residue as its fleet-wide index, and bank_count=40
    is a multiple of the dual-unit period, 5), clustering all 400 dual-unit homes into just 8 of the
    40 banks (1,000 kW of dual-unit inverter capacity on a 600 kVA bank). Offsetting the rule
    per-bank (`_is_dual_unit`, indexed by the hub's rank *within* its bank) must spread them 10 per
    bank, all 40 banks -- while keeping `i % bank_count` round-robin bank assignment, so every bank
    stays single-zone (feeder segments can't span zones)."""
    config = SimFleetTopologyConfig()  # hub_count=2000, bank_count=40, dual_unit_share=0.2 defaults
    topology = build_topology(config)

    dual_unit_bank_counts: dict[str, int] = {}
    for hub in topology.hubs:
        if hub.e_kwh == pytest.approx(config.e_kwh_dual_unit):
            dual_unit_bank_counts[hub.bank_id] = dual_unit_bank_counts.get(hub.bank_id, 0) + 1

    assert {b.bank_id for b in topology.banks} == set(dual_unit_bank_counts)  # every bank has some
    assert all(count == 10 for count in dual_unit_bank_counts.values())


def test_every_bank_has_exactly_one_zone():
    """Banks are feeder segments (module docstring): every hub assigned to a bank must share that
    bank's single zone. Round-robin bank assignment (`i % bank_count`) keeps this true because the
    zone cycle's period (`len(config.zones)`, 4) divides `bank_count` (40) at the confirmed defaults."""
    config = SimFleetTopologyConfig()  # hub_count=2000, bank_count=40, 4 zones
    topology = build_topology(config)

    zones_by_bank: dict[str, set[str]] = {}
    for hub in topology.hubs:
        zones_by_bank.setdefault(hub.bank_id, set()).add(hub.zone)

    assert {b.bank_id for b in topology.banks} == set(zones_by_bank)
    assert all(len(zones) == 1 for zones in zones_by_bank.values())


def test_bank_zone_is_majority_of_its_hubs():
    """Round-robin: bank-000 gets hubs 0, 3, 6 (zones cycle LZ_NORTH/LZ_SOUTH): zones LZ_NORTH,
    LZ_SOUTH, LZ_NORTH -> LZ_NORTH is the majority (2 of 3)."""
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


def test_every_seeded_bank_has_a_feeder_so_g06_evaluates():
    """K4/G-06 regression: G-06 only runs for a bank with a feeder_id, and the seed never set one, so the
    feeder ramp ceiling was inert. 40 banks x 4 zones -> 8 feeders of 5 same-zone banks."""
    topology = build_topology(SimFleetTopologyConfig())

    assert all(bank.feeder_id for bank in topology.banks)
    feeders: dict[str, list] = {}
    for bank in topology.banks:
        assert bank.feeder_id is not None
        feeders.setdefault(bank.feeder_id, []).append(bank)
    assert len(feeders) == 8
    for feeder_id, banks in feeders.items():
        assert len(banks) == BANKS_PER_FEEDER_DEFAULT
        assert {b.zone for b in banks} == {feeder_id.removeprefix("feeder-").rsplit("-", 1)[0]}
    bank_by_id = {b.bank_id: b for b in topology.banks}
    assert bank_by_id["bank-000"].feeder_id == "feeder-LZ_NORTH-00"
    assert bank_by_id["bank-016"].feeder_id == "feeder-LZ_NORTH-00"  # 5th LZ_NORTH bank
    assert bank_by_id["bank-020"].feeder_id == "feeder-LZ_NORTH-01"  # 6th LZ_NORTH bank


def test_feeder_grouping_is_configurable_and_deterministic():
    config = SimFleetTopologyConfig()
    one_per_bank = build_topology(config, banks_per_feeder=1)
    assert len({b.feeder_id for b in one_per_bank.banks}) == 40
    assert build_topology(config).banks == build_topology(config).banks
    with pytest.raises(ValueError, match="banks_per_feeder"):
        feeder_id_for("LZ_NORTH", 0, 0)


async def test_seed_writes_the_feeder_id():
    topology = build_topology(SimFleetTopologyConfig(hub_count=4, bank_count=2, zones=("LZ_NORTH",)))
    cursor = FakeCursor()

    await seed_topology(FakePool(cursor), topology)

    bank_params = [params for sql, params in cursor.executed if sql.strip().startswith("INSERT INTO og.bank")]
    assert [p["feeder_id"] for p in bank_params] == ["feeder-LZ_NORTH-00", "feeder-LZ_NORTH-00"]
