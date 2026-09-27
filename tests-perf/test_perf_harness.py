"""Unit tests for the pure parts of the perf harness (no stack, no DB, no broker, no Docker)."""

from __future__ import annotations

import math
import shutil
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import analyze
import perfenv
import sampler
import targets

REPO = Path(__file__).resolve().parents[1]
MQTT = {"host": "mosquitto", "port": 1883, "topic_root": "og/v1"}


@pytest.mark.parametrize(
    ("homes", "base", "block_banks"),
    [(1000, 550, 3), (2500, 1450, 7), (3500, 2000, 10), (5000, 2900, 14), (7500, 4350, 21)],
)
def test_fleet_shape_keeps_production_proportions(homes: int, base: int, block_banks: int) -> None:
    s = perfenv.fleet_shape(homes)
    assert (s.base_hubs, s.block_banks) == (base, block_banks)
    assert s.base_hubs + s.block_homes == homes
    assert s.total_hubs == homes + 9
    assert s.dual_unit_homes == homes // 5  # 20 %, 10 per 50-home bank


def test_production_size_reproduces_production_topology() -> None:
    s = perfenv.fleet_shape(3500)
    assert (s.base_banks, s.banks, s.total_hubs) == (40, 70, 3509)


def test_fleet_shape_rejects_ragged_tiny_or_out_of_scope_fleets() -> None:
    for bad in (1025, 250, 10000):
        with pytest.raises(ValueError):
            perfenv.fleet_shape(bad)


def test_base_config_is_isolated(tmp_path: Path) -> None:
    cfg = perfenv.render_orchestrator_toml(REPO, tmp_path)
    assert cfg["postgres"]["port"] == 5433 and cfg["postgres"]["database"] == "og_perf"
    assert cfg["mqtt"]["port"] == 11883 and cfg["mqtt"]["topic_root"] == "ogperf/v1"
    assert cfg["mqtt"]["client_id_prefix"] != "og"
    assert cfg["guardian"]["verdict_timeout_ms"] == 300  # tuning is production's, unchanged
    assert cfg["allocator"]["cycle_interval_s"] == 2


def test_isolation_check_refuses_production_endpoints(tmp_path: Path) -> None:
    cfg = perfenv.render_orchestrator_toml(REPO, tmp_path)
    cfg["postgres"]["port"] = 5432
    with pytest.raises(ValueError, match="postgres"):
        perfenv.check_isolated(cfg)
    cfg = perfenv.render_orchestrator_toml(REPO, tmp_path)
    cfg["guardian"]["key_path"] = "/etc/opengrid/guardian_ed25519.key"
    with pytest.raises(ValueError, match="/etc/opengrid"):
        perfenv.check_isolated(cfg)


def test_compose_config_keeps_production_tuning_with_dev_connectivity() -> None:
    cfg = perfenv.render_orchestrator_toml_compose(REPO)
    assert cfg["postgres"]["host"] == "postgres"
    assert cfg["mqtt"]["host"] == "mosquitto"
    assert cfg["feeds"]["ercot"]["base_url"].startswith("http://sim-market:8090")
    assert cfg["guardian"]["key_path"].startswith("/app/dev/keys/")
    assert cfg["guardian"]["verdict_timeout_ms"] == 300
    assert cfg["allocator"]["flow_limits"]["enabled"] is True  # production value, absent from docker.toml
    assert "trace" not in cfg


def test_write_compose_generates_everything(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    for sub in ("integration-sims/config", "orchestrator/config", "dev/config"):
        shutil.copytree(REPO / sub, repo / sub)
    (repo / "dev" / "secrets").write_text(
        "OG_DB_PASSWORD=a\nOG_API_PROXY_SECRET=b\nOG_MQTT_SIMCTL_PASSWORD=c\n"
    )
    gen, run = tmp_path / "gen", tmp_path / "run"
    shape = perfenv.write_compose(repo, run, 7500, gen)
    ports = perfenv.read_env(run / "ports.env")
    assert ports["TARGET"] == "compose" and ports["EXPECTED_HUBS"] == str(shape.total_hubs) == "7509"
    assert perfenv.read_env(run / "etc" / "secrets.env")["OG_MQTT_PERFMON_PASSWORD"] == "c"
    fleet = yaml.safe_load((gen / "fleet.perf.yaml").read_text())
    assert fleet["hub_count"] == 4350 and fleet["guardian_public_key_path_dev"].startswith("/app/dev/keys/")
    assert (gen / "config" / "authz.toml").exists()
    assert "topic read $SYS/#" in (gen / "opengrid.acl").read_text()


def test_write_compose_needs_dev_secrets(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "dev").mkdir(parents=True)
    with pytest.raises(ValueError, match="dev/secrets"):
        perfenv.write_compose(repo, tmp_path / "run", 1000, tmp_path / "gen")


def test_sim_configs_scale_blocks_and_base() -> None:
    out = perfenv.render_sim_configs(REPO, perfenv.fleet_shape(7500), mqtt=MQTT, keys={})
    fleet = out["fleet"]
    assert fleet["hub_count"] == 4350 and fleet["bank_count"] == 87
    enabled = {b["zone"]: b["banks"] for b in fleet["zone_blocks"] if b["enabled"]}
    assert enabled == {"LZ_AEN": 21, "LZ_LCRA": 21, "LZ_RAYBN": 21}
    assert out["scada"]["bank_count"] == 87


def test_acl_monitor_variants() -> None:
    base = perfenv.render_acl("ogperf/v1", monitor="og_perfmon")
    assert " og/v1/" not in base and "user og_perfmon" in base
    compose = perfenv.render_acl("og/v1", monitor="og_simctl")
    assert "user og_perfmon" not in compose
    simctl = compose.split("user og_simctl")[1].split("user ")[0]
    assert "topic read $SYS/#" in simctl


def test_secrets_generated_once_and_kept(tmp_path: Path) -> None:
    f = tmp_path / "etc" / "secrets.env"
    assert "OG_API_PROXY_SECRET" in perfenv.ensure_secrets(f)
    before = perfenv.read_env(f)
    assert perfenv.ensure_secrets(f) == []
    assert perfenv.read_env(f) == before
    assert before["ERCOT_PUBLIC_API_KEY_PRIMARY"] == before["OGSIM_MARKET_KEY_PRIMARY"]


def test_parse_metrics_keeps_selected_series() -> None:
    text = (
        "# HELP x\n"
        'og_control_tick_duration_seconds_bucket{le="0.1",phase="total"} 5.0\n'
        'og_engine_cycle_latency_ms{quantile="p99"} 42.5\n'
        "python_info 1\n"
    )
    assert sampler.parse_metrics(text) == {
        'og_control_tick_duration_seconds_bucket{le="0.1",phase="total"}': 5.0,
        'og_engine_cycle_latency_ms{quantile="p99"}': 42.5,
    }


def test_diskstats_and_host_parsing() -> None:
    text = (
        "   8       0 sda 100 0 800 50 200 0 1600 400 0 300 450 0 0 0 0\n"
        "   8       1 sda1 1 0 8 1 2 0 16 4 0 3 4 0 0 0 0\n"
        "   7       0 loop0 1 0 8 1 2 0 16 4 0 3 4 0 0 0 0\n"
        " 254       7 dm-7 5 0 40 2 9 0 72 8 0 6 10 0 0 0 0\n"
    )
    assert set(targets.parse_diskstats(text)) == {"sda"}
    named = targets.parse_diskstats(text, {"dm-7": "pgstandby"})
    assert named == {
        "pgstandby": {"reads": 5.0, "writes": 9.0, "sectors_w": 72.0, "write_ms": 8.0, "io_ms": 6.0}
    }
    h = targets.parse_host(
        "cpu 10 0 10 70 10 0 0 0\n", "MemTotal: 2048 kB\nMemAvailable: 1024 kB\n", "0.5 0.4 0.3 1/2 3"
    )
    assert h["cpu_total_ticks"] == 100 and h["cpu_idle_ticks"] == 80 and h["mem_available_mb"] == 1.0


def test_histogram_quantile_matches_promql() -> None:
    cum = [(0.1, 50.0), (0.25, 90.0), (0.5, 100.0), (math.inf, 100.0)]
    assert analyze.hist_quantile(0.5, cum) == pytest.approx(0.1)
    assert analyze.hist_quantile(0.7, cum) == pytest.approx(0.1 + 0.15 * 20 / 40)
    assert analyze.hist_quantile(0.99, cum) == pytest.approx(0.25 + 0.25 * 9 / 10)
    assert analyze.hist_quantile(0.5, []) is None


def test_bucket_delta_and_parse() -> None:
    a = {'h_bucket{le="1.0"}': 2.0, 'h_bucket{le="+Inf"}': 3.0}
    b = {'h_bucket{le="1.0"}': 5.0, 'h_bucket{le="+Inf"}': 9.0}
    assert analyze.bucket_delta(analyze.buckets(a, "h"), analyze.buckets(b, "h")) == [
        (1.0, 3.0),
        (math.inf, 6.0),
    ]


def test_slope_and_knee() -> None:
    assert analyze.slope_per_hour([(0, 100.0), (1800, 110.0), (3600, 120.0)]) == pytest.approx(20.0)
    knee = analyze.fit_knee([1000, 2000, 3000, 4000], [100.0, 200.0, 300.0, 400.0])
    assert knee["budget_crossing_hubs"] == 5000
    assert knee["p99_at_10k_ms"] == pytest.approx(1000.0, abs=1)


def test_percentile_nearest_rank() -> None:
    assert analyze.pct([5.0, 1.0, 3.0, 2.0, 4.0], 50) == 3.0
    assert analyze.pct([], 95) is None
