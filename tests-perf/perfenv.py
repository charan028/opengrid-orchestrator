#!/usr/bin/env python3
"""tests-perf/perfenv.py -- generate one isolated performance environment for a given fleet size.

    python tests-perf/perfenv.py --target compose --homes 7500          # Docker dev stack (any host)
    python tests-perf/perfenv.py --target base --homes 7500 --run DIR   # systemd units beside production
    python tests-perf/perfenv.py --homes 7500 --print-shape

Fleet shape (`fleet_shape`): production's 3,500 homes are a 2,000-home base fleet (40 banks, 4 ERCOT zones)
plus three enabled 10-bank zone blocks (LZ_AEN, LZ_LCRA, LZ_RAYBN; LZ_CPS off), 50 homes per bank, 20 %
dual-unit (the simulator's own dual-unit rule), one substation asset and 8 trucks. Other sizes keep those
proportions: each block gets round(10 * homes / 3500) banks and the base fleet takes the rest, always 50 homes
per bank. Total hubs = homes + 9, so --homes 3500 reproduces production's 3,509.

Both targets write <run>/ports.env (TARGET, endpoints, EXPECTED_HUBS) and <run>/etc/secrets.env (the DB
password, API proxy secret and monitor password the harness needs); the run directory defaults to
tests-perf/.run (gitignored).

- compose: tests-perf/compose/generated/ (gitignored) gets the sim configs, a config directory whose
  orchestrator.toml is production's tuning with the dev stack's hosts, keys and binds (dev/config/docker.toml),
  and the dev ACL plus `$SYS` read for og_simctl, the harness monitor. Credentials are the dev stack's own
  local values from dev/secrets.
- base: <run>/ gets the same files for the systemd stack (stack.sh): its own broker port/ACL/passwords,
  the 5433 test cluster, generated perf-only secrets. `check_isolated` refuses any rendered config that points
  at a production port, database, topic root or /etc path.
"""

from __future__ import annotations

import argparse
import copy
import secrets
import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomli_w
import yaml

HOMES_PER_BANK = 50
REF_HOMES = 3500
REF_BLOCK_BANKS = 10
ENABLED_BLOCKS = ("LZ_AEN", "LZ_LCRA", "LZ_RAYBN")
EXTRA_HUBS = 9  # sub-LZ_AEN-00 + 8 simulated trucks (fleet.yaml substation_assets / mobile_units)
MAX_HOMES = 7500  # the owner's scope for this campaign (10k is projected, not run)

HERE = Path(__file__).resolve().parent
DEFAULT_RUN = HERE / ".run"
COMPOSE_GEN = HERE / "compose" / "generated"
COMPOSE_APP_GEN = "/app/tests-perf/compose/generated"

# --- base target (systemd, beside production) -------------------------------------------------------------
BASE_TOPIC_ROOT = "ogperf/v1"
BASE_DB = {"DB_NAME": "og_perf", "DB_ROLE": "og_perf", "DB_PORT": 5433}
BASE_PORTS: dict[str, int] = {
    "MQTT_PORT": 11883,
    "API_PORT": 18080,
    "MARKET_PORT": 18090,
    "ENGINE_METRICS_PORT": 19101,
    "GUARDIAN_METRICS_PORT": 19103,
}
PRODUCTION_PORTS = frozenset({1883, 5432, 8080, 8090, 8091, 9101, 9103})
MQTT_ROLES = ("ENGINE", "GUARDIAN", "SIM", "API", "SAFESTOP", "SIMCTL")
GENERATED_SECRETS = (
    *(f"OG_MQTT_{r}_PASSWORD" for r in MQTT_ROLES),
    "OG_MQTT_PERFMON_PASSWORD",
    "OG_API_PROXY_SECRET",
    "OGSIM_MARKET_KEY_PRIMARY",
    "OGSIM_MARKET_KEY_SECONDARY",
    "OGSIM_MARKET_EIA_API_KEY",
    "ERCOT_API_PASSWORD",
)

# --- compose target (the Docker dev stack) ----------------------------------------------------------------
COMPOSE_PORTS: dict[str, int] = {
    "DB_PORT": 5432,  # published on 127.0.0.1 by dev/docker-compose.yml
    "MQTT_PORT": 1883,
    "API_PORT": 8080,
    "MARKET_PORT": 8090,
    "ENGINE_METRICS_PORT": 9101,  # inside the og-engine container (scraped through docker exec)
    "GUARDIAN_METRICS_PORT": 9103,  # inside the og-guardian container
}


@dataclass(frozen=True)
class FleetShape:
    homes: int
    base_hubs: int
    base_banks: int
    block_banks: int  # per enabled zone block

    @property
    def block_homes(self) -> int:
        return self.block_banks * HOMES_PER_BANK * len(ENABLED_BLOCKS)

    @property
    def banks(self) -> int:
        return self.base_banks + self.block_banks * len(ENABLED_BLOCKS)

    @property
    def total_hubs(self) -> int:
        return self.homes + EXTRA_HUBS

    @property
    def dual_unit_homes(self) -> int:
        # 0.2 share per 50-home bank = exactly 10 dual-unit homes per bank (simulator/seed rule).
        return self.banks * HOMES_PER_BANK // 5


def fleet_shape(homes: int) -> FleetShape:
    """Production-proportioned fleet for `homes` home hubs (see the module docstring)."""
    if homes % HOMES_PER_BANK:
        raise ValueError(f"homes must be a multiple of {HOMES_PER_BANK}, got {homes}")
    if homes > MAX_HOMES:
        raise ValueError(f"{homes} homes is above this campaign's {MAX_HOMES}-home scope")
    block_banks = max(1, round(REF_BLOCK_BANKS * homes / REF_HOMES))
    base_hubs = homes - block_banks * HOMES_PER_BANK * len(ENABLED_BLOCKS)
    if base_hubs < HOMES_PER_BANK * 4:
        raise ValueError(f"{homes} homes is too small for the production shape (base fleet {base_hubs})")
    return FleetShape(homes, base_hubs, base_hubs // HOMES_PER_BANK, block_banks)


def _zone_blocks(raw: list[dict[str, Any]], shape: FleetShape) -> list[dict[str, Any]]:
    out = []
    for block in raw:
        b = dict(block)
        b["enabled"] = b.get("zone") in ENABLED_BLOCKS
        b["banks"] = shape.block_banks if b["enabled"] else b.get("banks", REF_BLOCK_BANKS)
        b["homes_per_bank"] = HOMES_PER_BANK
        out.append(b)
    missing = set(ENABLED_BLOCKS) - {b.get("zone") for b in out}
    if missing:
        raise ValueError(f"zone blocks not declared in the repo sim config: {sorted(missing)}")
    return out


def render_sim_configs(
    repo: Path, shape: FleetShape, *, mqtt: dict[str, Any], keys: dict[str, str]
) -> dict[str, dict[str, Any]]:
    """fleet.yaml / scada.yaml for `shape`, from the repo's shipped configs (every other key kept)."""
    out: dict[str, dict[str, Any]] = {}
    for name in ("fleet", "scada"):
        cfg: dict[str, Any] = yaml.safe_load(
            (repo / "integration-sims" / "config" / f"{name}.yaml").read_text()
        )
        cfg["mqtt"] = dict(mqtt)
        cfg["bank_count"] = shape.base_banks
        cfg["zone_blocks"] = _zone_blocks(cfg.get("zone_blocks") or [], shape)
        if name == "fleet":
            cfg["hub_count"] = shape.base_hubs
            cfg.update(keys)
        out[name] = cfg
    return out


def _load_toml(path: Path) -> dict[str, Any]:
    return copy.deepcopy(tomllib.loads(path.read_text(encoding="utf-8")))


def render_orchestrator_toml(repo: Path, run: Path) -> dict[str, Any]:
    """Base target: production's orchestrator.toml with only what isolation needs changed (perf DB, broker,
    ports, the market simulator, perf keys, writable paths under the run directory). Tuning is kept."""
    cfg = _load_toml(repo / "orchestrator" / "config" / "orchestrator.toml")
    market = f"http://127.0.0.1:{BASE_PORTS['MARKET_PORT']}"
    var = run / "var"
    cfg["general"].update(env="perf", node_id="basepower-perf")
    cfg["postgres"].update(host="127.0.0.1", port=BASE_DB["DB_PORT"], database=BASE_DB["DB_NAME"])
    cfg["mqtt"].update(
        host="127.0.0.1", port=BASE_PORTS["MQTT_PORT"], topic_root=BASE_TOPIC_ROOT, client_id_prefix="ogperf"
    )
    ercot = cfg["feeds"]["ercot"]
    ercot.update(base_url=f"{market}/ercot", token_url=f"{market}/token", solar_by_region_enabled=False)
    ercot["products"] = [p for p in ercot["products"] if p != "np4-745-cd"]  # the market sim has no NP4-745
    cfg["feeds"]["eia"]["base_url"] = f"{market}/eia"
    cfg["feeds"]["nws"].update(
        base_url=f"{market}/nws", user_agent="OpenGrid-Orchestrator-Perf (perf@localhost)"
    )
    cfg["fleet"]["sim_config_path"] = str(run / "etc" / "sim" / "fleet.yaml")
    cfg["guardian"]["key_path"] = str(run / "keys" / "guardian_ed25519.key")
    cfg["safestop"]["key_path"] = str(run / "keys" / "safestop_ed25519.key")
    cfg["safestop"]["guardian_public_key_path"] = str(run / "keys" / "guardian_ed25519.pub")
    cfg["trace"].update(
        journal_path=str(var / "trace_journal.jsonl"),
        anchor_dir=str(var / "anchors"),
        anchor_secondary_dir=str(var / "anchors2"),
        anchor_key_path=str(run / "keys" / "trace_anchor_ed25519.key"),
    )
    cfg["health"]["engine_metrics_url"] = f"http://127.0.0.1:{BASE_PORTS['ENGINE_METRICS_PORT']}/metrics"
    cfg["health"]["guardian_metrics_url"] = f"http://127.0.0.1:{BASE_PORTS['GUARDIAN_METRICS_PORT']}/metrics"
    cfg["api"].update(bind_host="127.0.0.1", bind_port=BASE_PORTS["API_PORT"])
    cfg["api"]["csrf_allowed_hosts"] = [f"127.0.0.1:{BASE_PORTS['API_PORT']}"]
    cfg["metrics"].update(
        bind_host="127.0.0.1",
        engine_port=BASE_PORTS["ENGINE_METRICS_PORT"],
        guardian_port=BASE_PORTS["GUARDIAN_METRICS_PORT"],
    )
    cfg["pq_ingest"]["blob_store_dir"] = str(var / "pq_waveform")
    cfg["market_sim"]["base_url"] = market
    check_isolated(cfg)
    return cfg


def render_orchestrator_toml_compose(repo: Path) -> dict[str, Any]:
    """Compose target: production's tuning (orchestrator/config/orchestrator.toml) with the dev stack's
    connectivity (dev/config/docker.toml): hosts, feeds pointed at sim-market, dev keys, container binds.
    Production-only file paths are dropped so the dev defaults apply, exactly as on the dev stack."""
    cfg = _load_toml(repo / "orchestrator" / "config" / "orchestrator.toml")
    dock = _load_toml(repo / "dev" / "config" / "docker.toml")
    for section in ("general", "postgres", "mqtt", "feeds", "api", "metrics"):
        cfg[section] = copy.deepcopy(dock[section])
    cfg["general"]["node_id"] = "ogperf-compose"
    for key in ("key_path", "stop_release_authorised_operators"):
        cfg["guardian"][key] = dock["guardian"][key]
    for key in ("key_path", "guardian_public_key_path"):
        cfg["safestop"][key] = dock["safestop"][key]
    cfg["health"] = {**{k: v for k, v in cfg["health"].items() if not k.endswith("_url")}, **dock["health"]}
    cfg.pop("trace", None)
    cfg["pq_ingest"].pop("blob_store_dir", None)
    cfg["fleet"]["sim_config_path"] = f"{COMPOSE_APP_GEN}/fleet.perf.yaml"
    cfg["market_sim"]["base_url"] = "http://sim-market:8090"
    text = tomli_w.dumps(cfg)
    for needle in ("/etc/opengrid", "api.ercot.com", "api.eia.gov", "api.weather.gov"):
        if needle in text:
            raise ValueError(f"production reference left in the compose config: {needle}")
    return cfg


def check_isolated(cfg: dict[str, Any]) -> None:
    """Base target: refuse a config that could reach a production port, database, topic root or /etc path."""
    problems = []
    if cfg["postgres"]["port"] in PRODUCTION_PORTS or cfg["postgres"]["database"] == "og":
        problems.append("postgres points at production")
    if cfg["mqtt"]["port"] in PRODUCTION_PORTS or cfg["mqtt"]["topic_root"].startswith("og/"):
        problems.append("mqtt points at production")
    if cfg["api"]["bind_port"] in PRODUCTION_PORTS:
        problems.append("api.bind_port is a production port")
    for key in ("engine_port", "guardian_port"):
        if cfg["metrics"][key] in PRODUCTION_PORTS:
            problems.append(f"metrics.{key} is a production port")
    text = tomli_w.dumps(cfg)
    for needle in ("/etc/opengrid", "/var/lib/opengrid", "/srv/ogbackup", "api.ercot.com", "api.eia.gov"):
        if needle in text:
            problems.append(f"production reference left in config: {needle}")
    if problems:
        raise ValueError("; ".join(problems))


def render_acl(root: str, *, monitor: str) -> str:
    """Grants mirror production's (dev/scripts/gen_mosquitto_acl.py plus the og_api wave-request write
    production adds) at `root`. `monitor` is the harness's read-only user: `og_perfmon` (base, its own block)
    or an existing role that already reads `root/#` (compose: og_simctl), which only gains `$SYS` read."""
    grants: list[tuple[str, list[tuple[str, str]]]] = [
        (
            "og_sim",
            [
                ("write", "tel/#"),
                ("write", "ack/#"),
                ("read", "cmd/#"),
                ("read", "stop/#"),
                ("read", "lease/#"),
                ("write", "scada/#"),
                ("read", "scada/ctl/#"),
                ("readwrite", "scenario/#"),
            ],
        ),
        ("og_simctl", [("readwrite", "scenario/#"), ("write", "scada/#"), ("read", "#")]),
        ("og_engine", [("read", "tel/#"), ("read", "ack/#"), ("read", "scada/#"), ("read", "stop/#")]),
        ("og_guardian", [("write", "cmd/#"), ("write", "lease/#"), ("read", "ack/#"), ("read", "tel/#")]),
        ("og_safestop", [("write", "stop/#"), ("read", "ack/#")]),
        ("og_api", [("read", "#"), ("write", "scada/wave/+/+/+/request")]),
    ]
    lines = ["# GENERATED by tests-perf/perfenv.py (grants mirror production).", ""]
    for user, topics in grants:
        lines.append(f"user {user}")
        lines.extend(f"topic {access} {root}/{suffix}" for access, suffix in topics)
        if user == monitor:
            lines.append("topic read $SYS/#")
        lines.append("")
    if monitor == "og_perfmon":
        lines += [
            f"user {monitor}",
            f"topic read {root}/#",
            "topic read $SYS/#",
            f"topic write {root}/scenario/#",
            "",
        ]
    return "\n".join(lines)


def render_mosquitto_conf(run: Path) -> str:
    return "\n".join(
        [
            "# GENERATED by tests-perf/perfenv.py -- perf broker only (never /etc/mosquitto).",
            "per_listener_settings false",
            f"listener {BASE_PORTS['MQTT_PORT']} 127.0.0.1",
            "allow_anonymous false",
            f"password_file {run / 'mosquitto' / 'passwd'}",
            f"acl_file {run / 'mosquitto' / 'opengrid.acl'}",
            "persistence false",
            "log_dest stdout",
            "log_type error",
            "log_type warning",
            "log_type notice",
            "sys_interval 10",
            "",
        ]
    )


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    return values


def write_env_file(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.touch(mode=0o600)
    tmp.write_text("".join(f"{k}={v}\n" for k, v in sorted(values.items())), encoding="utf-8")
    tmp.replace(path)


def ensure_secrets(path: Path) -> list[str]:
    """Base target: generate every perf secret that is missing (existing values, e.g. OG_DB_PASSWORD from
    create_schema.sh, are kept). Returns the names generated now; values are never printed."""
    values = read_env(path)
    generated = [k for k in GENERATED_SECRETS if not values.get(k)]
    for key in generated:
        values[key] = secrets.token_hex(24)
    values.setdefault("ERCOT_API_USER", "perf@example.com")
    values["ERCOT_PUBLIC_API_KEY_PRIMARY"] = values["OGSIM_MARKET_KEY_PRIMARY"]
    values["ERCOT_PUBLIC_API_KEY_SECONDARY"] = values["OGSIM_MARKET_KEY_SECONDARY"]
    values["EIA_API_KEY"] = values["OGSIM_MARKET_EIA_API_KEY"]
    values["OGSIM_MARKET_TEST_USERS"] = f"{values['ERCOT_API_USER']}:{values['ERCOT_API_PASSWORD']}"
    write_env_file(path, values)
    return generated


def _yaml_out(path: Path, cfg: dict[str, Any], homes: int, shape: FleetShape) -> None:
    header = f"# GENERATED by tests-perf/perfenv.py for {homes} homes ({shape.total_hubs} hubs).\n"
    path.write_text(header + yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")


def _config_dir(repo: Path, dest: Path, cfg: dict[str, Any], source: str) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(repo / "orchestrator" / "config", dest)
    (dest / "orchestrator.toml").write_text(
        f"# GENERATED by tests-perf/perfenv.py from {source}.\n" + tomli_w.dumps(cfg), encoding="utf-8"
    )


def write_base(repo: Path, run: Path, homes: int) -> FleetShape:
    shape = fleet_shape(homes)
    (run / "etc" / "sim").mkdir(parents=True, exist_ok=True)
    for sub in ("keys", "mosquitto", "var/anchors", "var/anchors2", "var/pq_waveform", "logs", "data"):
        (run / sub).mkdir(parents=True, exist_ok=True)
    sims = render_sim_configs(
        repo,
        shape,
        mqtt={"host": "127.0.0.1", "port": BASE_PORTS["MQTT_PORT"], "topic_root": BASE_TOPIC_ROOT},
        keys={
            "guardian_public_key_path": str(run / "keys" / "guardian_ed25519.pub"),
            "safestop_public_key_path": str(run / "keys" / "safestop_ed25519.pub"),
        },
    )
    for name, cfg in sims.items():
        _yaml_out(run / "etc" / "sim" / f"{name}.yaml", cfg, homes, shape)
    _config_dir(
        repo, run / "config", render_orchestrator_toml(repo, run), "orchestrator/config/orchestrator.toml"
    )
    (run / "mosquitto" / "mosquitto.conf").write_text(render_mosquitto_conf(run))
    (run / "mosquitto" / "opengrid.acl").write_text(render_acl(BASE_TOPIC_ROOT, monitor="og_perfmon"))
    ensure_secrets(run / "etc" / "secrets.env")
    ports: dict[str, Any] = {
        "TARGET": "base",
        **BASE_PORTS,
        **BASE_DB,
        "TOPIC_ROOT": BASE_TOPIC_ROOT,
        "MONITOR_USER": "og_perfmon",
        "HOMES": homes,
        "EXPECTED_HUBS": shape.total_hubs,
    }
    (run / "ports.env").write_text("".join(f"{k}={v}\n" for k, v in ports.items()))
    return shape


def write_compose(repo: Path, run: Path, homes: int, gen: Path = COMPOSE_GEN) -> FleetShape:
    shape = fleet_shape(homes)
    dev_secrets = read_env(repo / "dev" / "secrets")
    missing = [
        k
        for k in ("OG_DB_PASSWORD", "OG_API_PROXY_SECRET", "OG_MQTT_SIMCTL_PASSWORD")
        if not dev_secrets.get(k)
    ]
    if missing:
        raise ValueError(
            f"dev/secrets lacks {missing}: bring the dev stack up once first (tests-perf/HANDOFF.md)"
        )
    gen.mkdir(parents=True, exist_ok=True)
    for sub in ("data", "logs", "etc"):
        (run / sub).mkdir(parents=True, exist_ok=True)
    sims = render_sim_configs(
        repo,
        shape,
        mqtt={"host": "mosquitto", "port": 1883, "topic_root": "og/v1"},
        keys={
            "guardian_public_key_path_dev": "/app/dev/keys/guardian-dev.pub",
            "safestop_public_key_path_dev": "/app/dev/keys/safestop-dev.pub",
        },
    )
    for name, cfg in sims.items():
        _yaml_out(gen / f"{name}.perf.yaml", cfg, homes, shape)
    _config_dir(
        repo,
        gen / "config",
        render_orchestrator_toml_compose(repo),
        "orchestrator.toml + dev/config/docker.toml",
    )
    (gen / "opengrid.acl").write_text(render_acl("og/v1", monitor="og_simctl"))
    write_env_file(
        run / "etc" / "secrets.env",
        {
            "OG_DB_PASSWORD": dev_secrets["OG_DB_PASSWORD"],
            "OG_API_PROXY_SECRET": dev_secrets["OG_API_PROXY_SECRET"],
            "OG_MQTT_PERFMON_PASSWORD": dev_secrets["OG_MQTT_SIMCTL_PASSWORD"],
        },
    )
    ports: dict[str, Any] = {
        "TARGET": "compose",
        "COMPOSE_PROJECT": "ogperf",
        **COMPOSE_PORTS,
        "DB_NAME": dev_secrets.get("POSTGRES_DB", "og"),
        "DB_ROLE": dev_secrets.get("POSTGRES_USER", "opengrid"),
        "TOPIC_ROOT": "og/v1",
        "MONITOR_USER": "og_simctl",
        "HOMES": homes,
        "EXPECTED_HUBS": shape.total_hubs,
    }
    (run / "ports.env").write_text("".join(f"{k}={v}\n" for k, v in ports.items()))
    return shape


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--homes", type=int, required=True, help="home hubs (multiple of 50, <= 7500); hubs = homes + 9"
    )
    p.add_argument("--target", choices=("compose", "base"), default="compose")
    p.add_argument("--run", type=Path, default=DEFAULT_RUN)
    p.add_argument("--repo", type=Path, default=HERE.parent)
    p.add_argument("--print-shape", action="store_true", help="print the fleet shape and exit")
    args = p.parse_args(argv)
    shape = fleet_shape(args.homes)
    if not args.print_shape:
        (write_compose if args.target == "compose" else write_base)(args.repo, args.run, args.homes)
    print(
        f"homes={shape.homes} hubs={shape.total_hubs} banks={shape.banks} base={shape.base_hubs}/"
        f"{shape.base_banks} banks, blocks {'/'.join(ENABLED_BLOCKS)} x {shape.block_banks} banks, "
        f"dual-unit homes={shape.dual_unit_homes}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
