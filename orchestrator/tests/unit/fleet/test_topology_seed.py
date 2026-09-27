"""dev/seed/topology_seed.py (WP-L, issue #39): the migration 0029 registry rows generated from the sim's
fleet.yaml + scada.yaml. Pure generation is tested here (no database): every hub, bank and feeder is covered,
each bank's units sum to its kVA rating (D-36), a dual-unit home behind a 50 kVA unit passes the real G-27 check, the SQL is idempotent, the zone
blocks are exactly the fleet seed's, the substation set and the trucks get their own transformer, and the
`--only-missing` backfill never rewrites a row. The database side is tests/integration/fleet/
test_topology_seed_db.py."""

from __future__ import annotations

import importlib.util
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

from opengrid.core.physics import HubParams
from opengrid.fleet import topology_audit
from opengrid.guardian import flow_checks
from opengrid.guardian.config import GuardianConfig
from opengrid.guardian.ports import HubSite, HubSnapshot, ProposedItem, ServiceTransformer

REPO = Path(__file__).resolve().parents[4]
_SCRIPT = REPO / "dev" / "seed" / "topology_seed.py"
_spec = importlib.util.spec_from_file_location("topology_seed", _SCRIPT)
assert _spec is not None and _spec.loader is not None
ts = importlib.util.module_from_spec(_spec)
sys.modules["topology_seed"] = ts
_spec.loader.exec_module(ts)

TERRITORY = {"LZ_AEN": "AUSTIN_ENERGY", "LZ_CPS": "CPS_ENERGY", "LZ_LCRA": "LCRA", "LZ_RAYBN": "RAYBURN"}  # D-37
DEV_FLEET = REPO / "dev" / "config" / "fleet.dev.yaml"
DEV_SCADA = REPO / "dev" / "config" / "scada.dev.yaml"


def production_like_configs(
    tmp: Path, enabled: tuple[str, ...] = ("LZ_AEN", "LZ_LCRA", "LZ_RAYBN")
) -> tuple[Path, Path]:
    """The shipped fleet.yaml/scada.yaml with exactly `enabled` zone blocks on (production's
    /etc/opengrid/sim override: LZ_AEN + D-32's LZ_LCRA/LZ_RAYBN, LZ_CPS off)."""
    out = []
    for name, src in (("fleet.yaml", ts.DEFAULT_FLEET_CONFIG), ("scada.yaml", ts.DEFAULT_SCADA_CONFIG)):
        data: dict[str, Any] = yaml.safe_load(src.read_text(encoding="utf-8"))
        for block in data.get("zone_blocks") or []:
            block["enabled"] = block["zone"] in enabled
        path = tmp / name
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        out.append(path)
    return out[0], out[1]


@pytest.fixture(scope="module")
def seed():
    """Production's shape: the base fleet plus LZ_AEN (market_model_seed.sql's substation set is there)."""
    return ts.build_seed(ts.load_fleet_config(blocks=["LZ_AEN"]), TERRITORY)


def test_default_blocks_are_exactly_the_fleet_seeds_enabled_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (ALR-XFMR-UNMAPPED on r3.4): the default used to enable every block scada.yaml DECLARES,
    LZ_CPS included although it is off. With production's LZ_AEN + LZ_LCRA + LZ_RAYBN that renumbered
    LZ_LCRA's banks as LZ_CPS's and gave them a CPS_ENERGY (regulated) HOME_BANK row. The default is now
    fleet.yaml's enabled blocks -- the ones `opengrid.fleet.seed` wrote to og.hub/og.bank."""
    from opengrid.fleet.seed import build_topology, load_sim_fleet_topology_config
    from opengrid.platform.config import Config

    monkeypatch.delenv("OG_FLEET_SIM_CONFIG", raising=False)
    fleet, scada = production_like_configs(tmp_path)
    seed = ts.build_seed(ts.load_fleet_config(fleet, scada), TERRITORY)
    zone_of = {b.bank_id: b.zone for b in seed.topology.banks}
    assert "LZ_CPS" not in zone_of.values()
    assert {zone_of[f"bank-{i:03d}"] for i in range(40, 50)} == {"LZ_AEN"}
    assert {zone_of[f"bank-{i:03d}"] for i in range(50, 60)} == {"LZ_LCRA"}
    assert {zone_of[f"bank-{i:03d}"] for i in range(60, 70)} == {"LZ_RAYBN"}
    assert len(seed.topology.hubs) == 3500 and len(zone_of) == 70
    assert {a.bank_id: a.utility_id for a in seed.assets}["bank-050"] == "LCRA"  # D-37: regulated, not CPS

    # Exactly the fleet seed's hubs and banks, id for id.
    fleet_seed = build_topology(
        load_sim_fleet_topology_config(Config({"fleet": {"sim_config_path": str(fleet)}}))
    )
    assert [(h.hub_id, h.bank_id, h.zone) for h in seed.topology.hubs] == [
        (h.hub_id, h.bank_id, h.zone) for h in fleet_seed.hubs
    ]
    assert [(b.bank_id, b.zone, b.feeder_id) for b in seed.topology.banks] == [
        (b.bank_id, b.zone, b.feeder_id) for b in fleet_seed.banks
    ]


def test_shipped_default_config_enables_what_fleet_yaml_enables(seed) -> None:
    config = ts.load_fleet_config()
    enabled_blocks = [b for b in config.zone_blocks if b.enabled]
    default = ts.build_seed(config, TERRITORY)
    assert len(default.topology.hubs) == config.hub_count + sum(
        b.banks * b.homes_per_bank for b in enabled_blocks
    )
    offset = config.bank_count
    banks = {b.bank_id: b.zone for b in default.topology.banks}
    for block in enabled_blocks:
        assert {banks[f"bank-{offset + i:03d}"] for i in range(block.banks)} == {block.zone}
        offset += block.banks
    for block in config.zone_blocks:
        if not block.enabled:
            assert block.zone not in banks.values()


@pytest.mark.parametrize(
    ("fleet", "scada"), [(ts.DEFAULT_FLEET_CONFIG, ts.DEFAULT_SCADA_CONFIG), (DEV_FLEET, DEV_SCADA)]
)
def test_every_hub_is_mapped_to_exactly_one_transformer_on_its_own_bank(fleet: Path, scada: Path) -> None:
    seed = ts.build_seed(ts.load_fleet_config(fleet, scada), TERRITORY)
    bank_of_hub = {h.hub_id: h.bank_id for h in seed.topology.hubs}
    mapped = Counter(hub_id for x in seed.transformers for hub_id in x.hub_ids)
    assert set(mapped) == set(bank_of_hub)
    assert set(mapped.values()) == {1}
    for x in seed.transformers:
        assert {bank_of_hub[h] for h in x.hub_ids} == {x.bank_id}
    assert len({x.transformer_id for x in seed.transformers}) == len(seed.transformers)


def test_d36_each_banks_transformers_sum_to_its_kva_rating_12_x_50_kva(seed) -> None:
    """D-36 (sizing per D-5): a 600 kVA, 50-home bank gets 12 x 50 kVA units summing to 600 kVA; group sizes
    differ by at most one home; the SQL rates each unit og.bank.kva_rating / units."""
    by_bank: dict[str, list[Any]] = {}
    for x in seed.transformers:
        by_bank.setdefault(x.bank_id, []).append(x)
    rating = {b.bank_id: b.kva_rating for b in seed.topology.banks}
    assert set(by_bank) == set(rating)
    for bank_id, units in by_bank.items():
        assert sum(x.rating_kva for x in units) == pytest.approx(rating[bank_id])
        assert {x.units for x in units} == {len(units)}
        sizes = [len(x.hub_ids) for x in units]
        assert max(sizes) - min(sizes) <= 1
    assert {len(u) for u in by_bank.values()} == {12}
    assert {x.rating_kva for x in seed.transformers} == {50.0}
    xfmr_sql = next(
        s.sql for s in ts.seed_statements(seed) if s.label == "og.service_transformer (home banks)"
    )
    assert "b.kva_rating / v.units" in xfmr_sql and "JOIN og.bank b ON b.bank_id = v.bank_id" in xfmr_sql


def test_dual_unit_homes_are_spread_one_per_transformer(seed) -> None:
    hubs = {h.hub_id: h for h in seed.topology.hubs}
    for x in seed.transformers:
        assert sum(1 for h in x.hub_ids if hubs[h].units == 2) <= 1
    per_bank = Counter(x.bank_id for x in seed.transformers if any(hubs[h].units == 2 for h in x.hub_ids))
    assert set(per_bank.values()) == {10}  # 10 dual-unit homes per bank on 10 distinct units


def test_sizing_rule() -> None:
    assert ts.transformer_count(600.0, 50) == 12
    assert ts.transformer_count(600.0, 5) == 5  # never more units than homes
    assert ts.transformer_count(625.0, 50) == 13
    assert ts.transformer_count(10.0, 3) == 1
    groups = ts.transformer_groups([f"hub-{i:05d}" for i in range(50)], 12)
    assert [len(g) for g in groups] == [5, 5] + [4] * 10
    assert groups[0] == ("hub-00000", "hub-00012", "hub-00024", "hub-00036", "hub-00048")
    assert sorted(h for g in groups for h in g) == [f"hub-{i:05d}" for i in range(50)]


def _policy() -> flow_checks.FlowPolicy:
    c = GuardianConfig(key_path="unused")
    return flow_checks.FlowPolicy(
        telemetry_required=c.flow_telemetry_required,
        max_age_s=c.flow_max_age_s,
        unknown_temp_factor=c.unknown_temp_factor,
        load_drop_kw=c.load_drop_kw,
        inverter_cap_kw=c.inverter_cap_kw,
        default_pv_rated_kw=c.default_pv_rated_kw,
        default_service_kw=c.default_service_kw,
        xfmr_forward_pct=c.xfmr_forward_pct,
        xfmr_reverse_pct=c.xfmr_reverse_pct,
        xfmr_max_stale_fraction=c.xfmr_max_stale_fraction,
        unmapped_xfmr_kva_per_home=c.unmapped_xfmr_kva_per_home,
    )


def test_a_20_kw_dual_unit_home_behind_a_50_kva_unit_is_feasible_and_the_group_binds(seed) -> None:
    """The real guardian G-27 check (default [guardian.flow] policy): the dual-unit home at its full 20 kW
    while its neighbours idle fits the 50 kVA unit; the whole group at full power (4 x 11 + 20 = 64 kW) does
    not -- the rating binds per group, and the bank's units together equal its 600 kVA rating."""
    hubs = {h.hub_id: h for h in seed.topology.hubs}
    candidates = [x for x in seed.transformers if any(hubs[h].p_kw == 20.0 for h in x.hub_ids)]
    assert candidates, "no transformer serves a dual-unit home"
    policy = _policy()
    for x in candidates:
        members = {
            h: flow_checks.TransformerMember(
                HubSnapshot(
                    params=HubParams(e_kwh=hubs[h].e_kwh, r_kwh=hubs[h].r_kwh, p_kw=hubs[h].p_kw),
                    soc_kwh=hubs[h].e_kwh * 0.8,
                    prev_p_kw=0.0,
                    health="online",
                ),
                HubSite(
                    export_limit_kw=None,
                    service_kw=None,
                    pv_rated_kw=0.0,
                    peak_kw=None,
                    tau_peak_s=None,
                    transformer_id=x.transformer_id,
                ),
            )
            for h in x.hub_ids
        }
        transformer = ServiceTransformer(x.transformer_id, x.rating_kva, tuple(sorted(x.hub_ids)))
        assert flow_checks.check_g27_transformer(transformer, members, -20.0, policy) == (True, None)
        full = -sum(hubs[h].p_kw for h in x.hub_ids)
        assert full < -x.rating_kva
        assert flow_checks.check_g27_transformer(transformer, members, full, policy)[0] is False
        assert flow_checks.check_g27_transformer(transformer, members, -x.rating_kva, policy) == (True, None)


def test_the_austin_substation_set_rows_are_never_overwritten(seed) -> None:
    sql = ts.render_sql(seed)
    assert "feeder-sub-LZ_AEN-00" not in sql and "sub-aen-01" not in sql
    protected = sql.split("-- Protected (market_model_seed.sql's row wins):\n", 1)[1].split(";", 1)[0]
    assert "'sub-LZ_AEN-00'" in protected and protected.rstrip().endswith("ON CONFLICT DO NOTHING")
    upserted = sql.split("-- Protected", 1)[0]
    assert "'sub-LZ_AEN-00'" not in upserted


def _statements(sql: str) -> list[str]:
    return [s.strip() for s in re.sub(r"--[^\n]*", "", sql).split(";") if s.strip()]


def test_sql_is_deterministic_and_every_write_is_idempotent(seed) -> None:
    again = ts.build_seed(ts.load_fleet_config(blocks=["LZ_AEN"]), TERRITORY)
    sql = ts.render_sql(seed)
    assert ts.render_sql(again) == sql
    statements = _statements(sql)
    inserts = [s for s in statements if s.startswith("INSERT")]
    # transformers (home, dedicated), feeders (generated, dedicated), substations (+ protected), assets
    assert len(inserts) == 7
    assert all("ON CONFLICT" in s for s in inserts)
    updates = [s for s in statements if s.startswith("UPDATE og.hub h SET transformer_id")]
    assert len(updates) == 2 and all("IS DISTINCT FROM" in u for u in updates)  # a re-run rewrites nothing
    assert statements[0] == "BEGIN" and statements[-1] == "COMMIT"


def test_the_substation_set_and_the_trucks_get_their_own_transformer_and_feeder(seed) -> None:
    """Dedicated-connection banks (SUBSTATION / MOBILE_STORAGE assets) are mapped -- trucks included, not
    exempt -- to one transformer at the bank's own kVA rating (600 kVA depot service for a 500 kW truck,
    20,408 kVA for the 20 MW set), and their feeder gets a limit row unless one exists."""
    by_label = {s.label: s.sql for s in ts.seed_statements(seed)}
    xfmr = by_label["og.service_transformer (dedicated banks)"]
    assert "'xfmr-' || b.bank_id || '-00', b.bank_id, b.kva_rating" in xfmr
    assert topology_audit.DEDICATED_BANKS_SQL in xfmr
    assert "'SUBSTATION', 'MOBILE_STORAGE'" in topology_audit.DEDICATED_BANKS_SQL
    hubs = by_label["og.hub.transformer_id (dedicated banks)"]
    assert "SET transformer_id = 'xfmr-' || b.bank_id || '-00'" in hubs
    feeders = by_label["og.feeder_limit (dedicated banks)"]
    assert "sum(b.kva_rating)" in feeders and feeders.rstrip().endswith("ON CONFLICT DO NOTHING")


def test_only_missing_is_insert_only(seed) -> None:
    """The production backfill: no DO UPDATE anywhere, a hub is mapped only while its transformer_id is NULL."""
    sql = ts.render_sql(seed, only_missing=True)
    statements = _statements(sql)
    inserts = [s for s in statements if s.startswith("INSERT")]
    assert len(inserts) == 7
    assert all(s.endswith("DO NOTHING") for s in inserts)
    assert "DO UPDATE" not in sql
    updates = [s for s in statements if s.startswith("UPDATE og.hub h SET transformer_id")]
    assert len(updates) == 2
    assert all("h.transformer_id IS NULL" in u and "IS DISTINCT FROM" not in u for u in updates)
    # Same rows as the full seed, only the conflict handling differs.
    assert [s.label for s in ts.seed_statements(seed, only_missing=True)] == [
        s.label for s in ts.seed_statements(seed)
    ]


def test_the_backfill_guard_flags_any_changed_or_removed_row() -> None:
    before = {"og.bank": {"bank-000": "a"}, "og.hub.transformer_id (already set)": {"hub-00000": "x-1"}}
    same_plus_new = {
        "og.bank": {"bank-000": "a", "bank-001": "b"},
        "og.hub.transformer_id (already set)": {"hub-00000": "x-1", "hub-00001": "x-1"},
    }
    assert ts._changed_existing(before, same_plus_new) == []
    changed = {"og.bank": {"bank-000": "CHANGED"}, "og.hub.transformer_id (already set)": {}}
    assert ts._changed_existing(before, changed) == ["og.bank", "og.hub.transformer_id (already set)"]


def test_the_backfill_guard_covers_every_topology_table() -> None:
    guarded = " ".join(ts._GUARDED_ROWS)
    for table in (
        "og.bank",
        "og.hub",
        "og.service_transformer",
        "og.feeder_limit",
        "og.substation_limit",
        "og.asset",
    ):
        assert table in guarded
    hub = ts._GUARDED_ROWS["og.hub (all but the NULL-fillable columns)"]
    assert "- 'transformer_id' - 'service_kw' - 'export_limit_kw'" in hub
    for col in ("transformer_id", "service_kw", "export_limit_kw"):
        assert f"WHERE {col} IS NOT NULL" in ts._GUARDED_ROWS[f"og.hub.{col} (already set)"]


def test_cli_writes_the_same_sql_to_a_file(tmp_path: Path) -> None:
    out = tmp_path / "topology.sql"
    assert ts.main(["--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == ts.render_sql(
        ts.build_seed(ts.load_fleet_config(), ts.load_zone_territory(ts.DEFAULT_TDSP_TARIFFS))
    )
    assert ts.main(["--out", str(out), "--only-missing"]) == 0
    assert "DO UPDATE" not in out.read_text(encoding="utf-8")


def test_dry_run_needs_a_database() -> None:
    with pytest.raises(SystemExit):
        ts.main(["--dry-run"])


def test_blocks_add_to_fleet_yamls_enabled_blocks_never_remove() -> None:
    """`--blocks` only ADDS blocks (what-if SQL): an explicit `blocks=[]` is the fleet.yaml default."""
    config = ts.load_fleet_config(blocks=[])
    assert config == ts.load_fleet_config()
    none = ts.build_seed(config, TERRITORY)
    enabled_blocks = [b for b in config.zone_blocks if b.enabled]
    banks_per_zone = config.bank_count // len(config.zones)
    base_feeders = len(config.zones) * -(-banks_per_zone // ts.BANKS_PER_FEEDER_DEFAULT)
    expected_banks = config.bank_count + sum(b.banks for b in enabled_blocks)
    expected_feeders = base_feeders + sum(-(-b.banks // ts.BANKS_PER_FEEDER_DEFAULT) for b in enabled_blocks)
    assert len(none.topology.banks) == expected_banks
    assert len(none.feeders) == expected_feeders
    with_aen = ts.load_fleet_config(blocks=["LZ_AEN"])
    assert {b.zone for b in with_aen.zone_blocks if b.enabled} == {b.zone for b in enabled_blocks} | {
        "LZ_AEN"
    }
    with pytest.raises(ValueError, match=r"not in fleet.yaml"):
        ts.load_fleet_config(blocks=["LZ_NOWHERE"])


def test_fleet_and_scada_configs_must_agree(tmp_path: Path) -> None:
    scada = tmp_path / "scada.yaml"
    scada.write_text("bank_count: 41\nzones: [LZ_NORTH, LZ_SOUTH, LZ_HOUSTON, LZ_WEST]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="bank_count"):
        ts.load_fleet_config(ts.DEFAULT_FLEET_CONFIG, scada)


def test_fleet_and_scada_must_enable_the_same_zone_blocks(tmp_path: Path) -> None:
    fleet, _ = production_like_configs(tmp_path)
    (tmp_path / "other").mkdir()
    _, scada = production_like_configs(
        tmp_path / "other", enabled=("LZ_AEN", "LZ_CPS", "LZ_LCRA", "LZ_RAYBN")
    )
    with pytest.raises(ValueError, match="zone block LZ_CPS"):
        ts.load_fleet_config(fleet, scada)


def test_unmapped_queries_are_counts_covering_both_guardian_alerts() -> None:
    labels = " ".join(topology_audit.UNMAPPED_QUERIES)
    assert "ALR-XFMR-UNMAPPED" in labels and "ALR-BANK-UNMAPPED-TOPOLOGY" in labels
    assert all(q.startswith("SELECT count(") for q in topology_audit.UNMAPPED_QUERIES.values())

    class _Conn:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def execute(self, query: str) -> _Conn:
            self.queries.append(query)
            return self

        def fetchone(self) -> tuple[int]:
            return (len(self.queries) - 1,)

    conn = _Conn()
    counts = topology_audit.count_unmapped(conn)  # type: ignore[arg-type]
    assert list(counts) == list(topology_audit.UNMAPPED_QUERIES)
    assert list(counts.values()) == list(range(len(counts)))


def test_dedicated_hubs_get_poi_premise_only_where_null(seed) -> None:
    """Toll hotfix: sub-LZ_AEN-00 (and each truck) gets service_kw = export_limit_kw = its nameplate, only
    where og.hub has NULL -- never overwritten, in the full seed and the backfill alike."""
    for only_missing in (False, True):
        premise = next(
            s.sql
            for s in ts.seed_statements(seed, only_missing=only_missing)
            if s.label.startswith("og.hub service_kw/export_limit_kw")
        )
        assert "service_kw = coalesce(h.service_kw, CASE WHEN EXISTS" in premise
        assert (
            "THEN b.kva_rating ELSE h.p_kw END" in premise
            and "export_limit_kw = coalesce(h.export_limit_kw," in premise
        )
        assert "(h.service_kw IS NULL OR h.export_limit_kw IS NULL)" in premise
        assert topology_audit.DEDICATED_BANKS_SQL in premise
    assert any("POI premise" in label for label in topology_audit.UNMAPPED_QUERIES)


def test_only_one_hub_is_separable_and_insert_only() -> None:
    statements = ts._dedicated_statements(only_missing=True, only_hub="sub-LZ_AEN-00")
    assert [s.label for s in statements] == [
        "og.service_transformer (dedicated banks)",
        "og.hub.transformer_id (dedicated banks)",
        "og.hub service_kw/export_limit_kw (dedicated banks, NULL only)",
        "og.feeder_limit (dedicated banks)",
    ]
    for s in statements:
        assert "o.hub_id = 'sub-LZ_AEN-00'" in s.sql
        assert "DO UPDATE" not in s.sql and "IS DISTINCT FROM" not in s.sql
    with pytest.raises(SystemExit):
        ts.main(["--only", "sub-LZ_AEN-00"])  # needs --dsn


def _substation_site(export_kw: float | None, service_kw: float | None) -> HubSite:
    return HubSite(
        export_limit_kw=export_kw,
        service_kw=service_kw,
        pv_rated_kw=GuardianConfig(key_path="unused").default_pv_rated_kw,  # og.hub.pv_rated_kw is NULL
        peak_kw=None,
        tau_peak_s=None,
        transformer_id="xfmr-bank-sub-LZ_AEN-00-00",
    )


def _substation_hub() -> HubSnapshot:
    return HubSnapshot(
        params=HubParams(e_kwh=40000.0, r_kwh=8000.0, p_kw=20000.0),
        soc_kwh=32000.0,
        prev_p_kw=0.0,
        health="online",
    )


def _step(kw: float) -> ProposedItem:
    return ProposedItem(hub_id="sub-LZ_AEN-00", p_kw_setpoint=-kw, reason_code="SELECTOR")


def test_g27_passes_a_20_mw_step_on_the_substation_sets_own_transformer() -> None:
    transformer = ServiceTransformer("xfmr-bank-sub-LZ_AEN-00-00", 20408.0, ("sub-LZ_AEN-00",))
    members = {
        "sub-LZ_AEN-00": flow_checks.TransformerMember(_substation_hub(), _substation_site(20000.0, 20000.0))
    }
    assert flow_checks.check_g27_transformer(transformer, members, -20000.0, _policy()) == (True, None)


def test_g26_with_the_poi_premise_passes_the_toll_step_the_null_premise_vetoes() -> None:
    """The toll-hotfix blocker: with og.hub's premise NULL (r3.4 prod) the guardian uses a home's defaults
    (export 20 kW) and G-26 vetoes any MW step; with service_kw = export_limit_kw = 20,000 kW it passes a step
    up to the nameplate less G-26's `load_drop_kw` margin (static meter)."""
    policy = _policy()
    hub, fixed = _substation_hub(), _substation_site(20000.0, 20000.0)
    step = 20000.0 - policy.load_drop_kw
    assert flow_checks.check_g26_home_meter(_step(step), hub, fixed, policy).ok
    assert not flow_checks.check_g26_home_meter(_step(step), hub, _substation_site(None, None), policy).ok
    assert not flow_checks.check_g26_home_meter(_step(1000.0), hub, _substation_site(None, None), policy).ok


def test_g26_passes_the_full_20_mw_step_with_the_premise_at_the_transformer_rating() -> None:
    """Owner decision: sub-LZ_AEN-00's export/service premise is its 20,408 kVA rating (placeholder for the
    grid-connection agreement), so the full 20,000 kW step clears G-26's load-drop margin."""
    policy = _policy()
    assert flow_checks.check_g26_home_meter(
        _step(20000.0), _substation_hub(), _substation_site(20408.0, 20408.0), policy
    ).ok
