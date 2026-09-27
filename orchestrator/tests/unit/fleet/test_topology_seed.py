"""dev/seed/topology_seed.py (WP-L, issue #39): the migration 0029 registry rows generated from the sim's
fleet.yaml + scada.yaml. Pure generation is tested here (no database): every hub, bank and feeder is covered,
a dual-unit home behind a 25 kVA transformer passes the real G-27 check, and the SQL is idempotent."""

from __future__ import annotations

import importlib.util
import re
import sys
from collections import Counter
from pathlib import Path

import pytest

from opengrid.core.physics import HubParams
from opengrid.guardian import flow_checks
from opengrid.guardian.config import GuardianConfig
from opengrid.guardian.ports import HubSite, HubSnapshot, ServiceTransformer

REPO = Path(__file__).resolve().parents[4]
_SCRIPT = REPO / "dev" / "seed" / "topology_seed.py"
_spec = importlib.util.spec_from_file_location("topology_seed", _SCRIPT)
assert _spec is not None and _spec.loader is not None
ts = importlib.util.module_from_spec(_spec)
sys.modules["topology_seed"] = ts
_spec.loader.exec_module(ts)

TERRITORY = {"LZ_AEN": "AUSTIN_ENERGY", "LZ_CPS": "CPS_ENERGY"}
DEV_FLEET = REPO / "dev" / "config" / "fleet.dev.yaml"
DEV_SCADA = REPO / "dev" / "config" / "scada.dev.yaml"


@pytest.fixture(scope="module")
def seed():
    return ts.build_seed(ts.load_fleet_config(), TERRITORY)


def test_default_blocks_are_scada_covered_and_match_add_austin_fleet(seed) -> None:
    """OWNER DECISION D-32, 2026-09-26: LZ_LCRA/LZ_RAYBN (free-market ERCOT zones) are switched ON in
    the shipped fleet.yaml/scada.yaml, alongside LZ_AEN/LZ_CPS (regulated, still off in the shipped
    default -- production enables AEN via its own override). `load_fleet_config()`'s default `--blocks`
    set is "every zone scada.yaml declares" (this module's own docstring), so the shared `seed`
    fixture's scope is whatever the shipped configs currently enable -- computed here from the loaded
    config itself (`config.zone_blocks`), not hardcoded, so this test tracks whichever blocks are
    enabled without needing an update every time that changes. Each enabled block's ids are exactly
    dev/seed/add_austin_fleet.sql's scheme extended in `zone_blocks:` list order (bank-040..049
    LZ_AEN, bank-050..059 LZ_CPS, bank-060..069 LZ_LCRA, bank-070..079 LZ_RAYBN, when enabled)."""
    config = ts.load_fleet_config()
    enabled_blocks = [b for b in config.zone_blocks if b.enabled]
    expected_hub_count = config.hub_count + sum(b.banks * b.homes_per_bank for b in enabled_blocks)
    expected_bank_count = config.bank_count + sum(b.banks for b in enabled_blocks)

    banks = {b.bank_id: b.zone for b in seed.topology.banks}
    assert len(seed.topology.hubs) == expected_hub_count
    assert len(banks) == expected_bank_count

    offset = config.bank_count
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
        assert ts.MIN_HOMES_PER_TRANSFORMER <= len(x.hub_ids) <= ts.MAX_HOMES_PER_TRANSFORMER
        assert x.rating_kva in (25.0, 50.0, 75.0)
    assert len({x.transformer_id for x in seed.transformers}) == len(seed.transformers)


def test_every_bank_has_transformers_a_feeder_and_a_substation(seed) -> None:
    banks = {b.bank_id for b in seed.topology.banks}
    assert {x.bank_id for x in seed.transformers} == banks
    assets = {a.bank_id: a for a in seed.assets}
    assert set(assets) == banks
    substations = {s.substation_id for s in seed.substations}
    feeder_limits = {f.feeder_id for f in seed.feeders}
    for bank in seed.topology.banks:
        assert bank.feeder_id in feeder_limits
        assert assets[bank.bank_id].feeder_id == bank.feeder_id
        assert assets[bank.bank_id].substation_id in substations
        assert assets[bank.bank_id].utility_id == TERRITORY.get(bank.zone)


def test_every_feeder_has_limits_eight_competitive_plus_the_austin_block(seed) -> None:
    feeders = {f.feeder_id: f for f in seed.feeders}
    competitive = {f for f in feeders if f.split("-")[1] in ("LZ_NORTH", "LZ_SOUTH", "LZ_HOUSTON", "LZ_WEST")}
    assert len(competitive) == 8
    assert {"feeder-LZ_AEN-00", "feeder-LZ_AEN-01"} <= set(feeders)
    assert {b.feeder_id for b in seed.topology.banks} == set(feeders)
    for f in feeders.values():
        assert f.thermal_kw == ts.FEEDER_THERMAL_KW
        assert f.reverse_kw == pytest.approx(5 * 600.0)  # five 600 kVA segments
    covered = {f for s in seed.substations for f in s.feeder_ids}
    assert covered == set(feeders)


def test_a_20_kw_dual_unit_home_behind_a_25_kva_transformer_is_feasible(seed) -> None:
    """The real guardian G-27 check (default [guardian.flow] policy): the dual-unit home discharging at its
    full 20 kW while its transformer neighbours idle stays inside the 25 kVA reverse rating."""
    hubs = {h.hub_id: h for h in seed.topology.hubs}
    candidates = [
        x for x in seed.transformers if x.rating_kva == 25.0 and any(hubs[h].p_kw == 20.0 for h in x.hub_ids)
    ]
    assert candidates, "no 25 kVA transformer serves a dual-unit home"
    c = GuardianConfig(key_path="unused")
    policy = flow_checks.FlowPolicy(
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
        # ...and the rating binds: a second full-power home on the same transformer would not fit.
        assert flow_checks.check_g27_transformer(transformer, members, -31.0, policy)[0] is False


def test_sizing_rule() -> None:
    assert ts.transformer_group_sizes(50) == ts.TRANSFORMER_GROUP_PATTERN
    assert sum(ts.TRANSFORMER_GROUP_PATTERN) == 50
    for homes in range(4, 200):
        sizes = ts.transformer_group_sizes(homes)
        assert sum(sizes) == homes
        assert all(4 <= s <= 10 for s in sizes)
    assert [ts.transformer_kva_for(n) for n in (4, 5, 6, 8, 9, 10)] == [25.0, 25.0, 50.0, 50.0, 75.0, 75.0]


def test_the_austin_substation_set_rows_are_never_overwritten(seed) -> None:
    sql = ts.render_sql(seed)
    assert "feeder-sub-LZ_AEN-00" not in sql and "sub-aen-01" not in sql
    protected = sql.split("-- Protected (market_model_seed.sql's row wins):\n", 1)[1].split(";", 1)[0]
    assert "'sub-LZ_AEN-00'" in protected and protected.rstrip().endswith("ON CONFLICT DO NOTHING")
    upserted = sql.split("-- Protected", 1)[0]
    assert "'sub-LZ_AEN-00'" not in upserted


def test_sql_is_deterministic_and_every_write_is_idempotent(seed) -> None:
    again = ts.build_seed(ts.load_fleet_config(), TERRITORY)
    sql = ts.render_sql(seed)
    assert ts.render_sql(again) == sql
    statements = [s.strip() for s in re.sub(r"--[^\n]*", "", sql).split(";") if s.strip()]
    inserts = [s for s in statements if s.startswith("INSERT")]
    assert len(inserts) == 5  # transformers, feeders, substations, protected substation, assets
    assert all("ON CONFLICT" in s for s in inserts)
    update = next(s for s in statements if s.startswith("UPDATE og.hub"))
    assert "IS DISTINCT FROM" in update  # a re-run rewrites nothing
    assert statements[0] == "BEGIN" and statements[-1] == "COMMIT"


def test_cli_writes_the_same_sql_to_a_file(tmp_path: Path) -> None:
    out = tmp_path / "topology.sql"
    assert ts.main(["--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == ts.render_sql(
        ts.build_seed(ts.load_fleet_config(), ts.load_zone_territory(ts.DEFAULT_TDSP_TARIFFS))
    )


def test_blocks_can_be_narrowed_or_dropped() -> None:
    """OWNER DECISION D-32, 2026-09-26: LZ_LCRA/LZ_RAYBN are enabled directly in the shipped fleet.yaml
    (`enabled: true`), not merely opted into via `--blocks` -- `load_fleet_config`'s own `b.enabled or
    b.zone in wanted` means an explicit `blocks=[]` can no longer turn OFF a block the YAML itself
    already turned on (`--blocks`/`blocks=` can only ADD to what's already enabled, never subtract).
    Expected bank/feeder counts are computed from the loaded config's own zone_blocks (not hardcoded),
    so this test tracks whichever blocks the shipped YAML enables without needing a manual update."""
    config = ts.load_fleet_config(blocks=[])
    none = ts.build_seed(config, TERRITORY)
    enabled_blocks = [b for b in config.zone_blocks if b.enabled]
    banks_per_zone = config.bank_count // len(config.zones)
    base_feeders = len(config.zones) * -(-banks_per_zone // ts.BANKS_PER_FEEDER_DEFAULT)
    expected_banks = config.bank_count + sum(b.banks for b in enabled_blocks)
    expected_feeders = base_feeders + sum(-(-b.banks // ts.BANKS_PER_FEEDER_DEFAULT) for b in enabled_blocks)
    assert len(none.topology.banks) == expected_banks
    assert len(none.feeders) == expected_feeders
    with pytest.raises(ValueError, match=r"not in fleet.yaml"):
        ts.load_fleet_config(blocks=["LZ_NOWHERE"])


def test_fleet_and_scada_configs_must_agree(tmp_path: Path) -> None:
    scada = tmp_path / "scada.yaml"
    scada.write_text("bank_count: 41\nzones: [LZ_NORTH, LZ_SOUTH, LZ_HOUSTON, LZ_WEST]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="bank_count"):
        ts.load_fleet_config(ts.DEFAULT_FLEET_CONFIG, scada)
