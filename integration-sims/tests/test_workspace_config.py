"""A non-production sim run (an agent workspace, `OG_WS`, or anything without the production marker
`OGSIM_ENV=prod`) must never fall back to production MQTT settings: the production topic root `og/v1`
or the production guardian/safestop public keys. Environment variables always win over the YAML, and
the default YAML is the one shipped with the package, whatever the working directory."""

from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path

import pytest

from ogsim.common import config as sim_config
from ogsim.common.config import (
    DEFAULT_FLEET_CONFIG_PATH,
    DEFAULT_SCADA_CONFIG_PATH,
    PRODUCTION_TOPIC_ROOT,
    WorkspaceConfigError,
    load_fleet_config,
    load_scada_config,
    resolve_topic_root,
)


@pytest.fixture
def workspace(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("OG_WS", "guardsafe")
    monkeypatch.setenv("OG_MQTT_WS_USER", "ogw_guardsafe")
    monkeypatch.setenv("OG_MQTT_WS_PASSWORD", "ws-test-password")
    for name in (
        "OG_MQTT_ROOT",
        "OGSIM_ENV",
        "OGSIM_GUARDIAN_PUBLIC_KEY_PATH",
        "OGSIM_SAFESTOP_PUBLIC_KEY_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def dev(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Neither a workspace nor marked production (e.g. a developer shell)."""
    for name in ("OG_WS", "OG_MQTT_ROOT", "OGSIM_ENV", "OGSIM_GUARDIAN_PUBLIC_KEY_PATH", "OG_MQTT_WS_USER"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def production(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in (
        "OG_WS",
        "OG_MQTT_ROOT",
        "OGSIM_GUARDIAN_PUBLIC_KEY_PATH",
        "OGSIM_SAFESTOP_PUBLIC_KEY_PATH",
        "OG_MQTT_WS_USER",
        "OG_MQTT_WS_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OGSIM_ENV", "prod")
    return monkeypatch


# --- topic root ---------------------------------------------------------------------------------------


def test_workspace_without_a_topic_root_refuses_to_start(workspace: pytest.MonkeyPatch) -> None:
    with pytest.raises(WorkspaceConfigError, match="OG_MQTT_ROOT"):
        resolve_topic_root({})


def test_workspace_refuses_the_yaml_production_root(workspace: pytest.MonkeyPatch) -> None:
    """fleet.yaml/scada.yaml ship `topic_root: og/v1`; it used to win over OG_MQTT_ROOT."""
    with pytest.raises(WorkspaceConfigError):
        resolve_topic_root({"topic_root": PRODUCTION_TOPIC_ROOT})


def test_workspace_may_not_point_at_the_production_root(workspace: pytest.MonkeyPatch) -> None:
    workspace.setenv("OG_MQTT_ROOT", "og/v1/")
    with pytest.raises(WorkspaceConfigError):
        resolve_topic_root({})


def test_env_root_always_wins_over_yaml(workspace: pytest.MonkeyPatch) -> None:
    workspace.setenv("OG_MQTT_ROOT", "ogtest/guardsafe")
    assert resolve_topic_root({"topic_root": PRODUCTION_TOPIC_ROOT}) == "ogtest/guardsafe"


def test_unmarked_process_refuses_the_production_root(dev: pytest.MonkeyPatch) -> None:
    with pytest.raises(WorkspaceConfigError, match="not marked production"):
        resolve_topic_root({"topic_root": PRODUCTION_TOPIC_ROOT})
    with pytest.raises(WorkspaceConfigError):
        resolve_topic_root({})


def test_a_workspace_is_never_production(workspace: pytest.MonkeyPatch) -> None:
    workspace.setenv("OGSIM_ENV", "prod")
    workspace.setenv("OG_MQTT_ROOT", "og/v1")
    with pytest.raises(WorkspaceConfigError, match="never production"):
        resolve_topic_root({})


def test_production_keeps_the_production_root(production: pytest.MonkeyPatch) -> None:
    assert resolve_topic_root({}) == PRODUCTION_TOPIC_ROOT
    assert resolve_topic_root({"topic_root": "og/v1"}) == "og/v1"


def test_env_host_and_port_win_over_yaml(workspace: pytest.MonkeyPatch) -> None:
    workspace.setenv("OG_MQTT_ROOT", "ogtest/guardsafe")
    workspace.setenv("OG_MQTT_HOST", "10.0.0.9")
    workspace.setenv("OG_MQTT_PORT", "18883")
    settings = sim_config.mqtt_settings_from_env({"host": "127.0.0.1", "port": 1883})
    assert (settings.host, settings.port, settings.topic_root) == ("10.0.0.9", 18883, "ogtest/guardsafe")


# --- public keys ---------------------------------------------------------------------------------------


def test_workspace_never_trusts_the_production_public_keys(workspace: pytest.MonkeyPatch) -> None:
    workspace.setenv("OG_MQTT_ROOT", "ogtest/guardsafe")
    config = load_fleet_config("does-not-exist.yaml")
    with pytest.raises(WorkspaceConfigError, match="guardian"):
        config.public_key_path()
    with pytest.raises(WorkspaceConfigError, match="safestop"):
        config.safestop_key_path()


def test_key_path_overrides_from_env(workspace: pytest.MonkeyPatch) -> None:
    workspace.setenv("OG_MQTT_ROOT", "ogtest/guardsafe")
    workspace.setenv("OGSIM_GUARDIAN_PUBLIC_KEY_PATH", "/tmp/ws/guardian.pub")
    workspace.setenv("OGSIM_SAFESTOP_PUBLIC_KEY_PATH", "/tmp/ws/safestop.pub")
    config = load_fleet_config("does-not-exist.yaml")
    assert config.public_key_path() == "/tmp/ws/guardian.pub"
    assert config.safestop_key_path() == "/tmp/ws/safestop.pub"


def test_production_keeps_the_production_key_paths(production: pytest.MonkeyPatch) -> None:
    config = load_fleet_config()
    assert config.public_key_path() == "/etc/opengrid/guardian_ed25519.pub"
    assert config.safestop_key_path() == "/etc/opengrid/safestop_ed25519.pub"
    assert replace(config, guardian_public_key_path_dev="/dev.pub").public_key_path() == "/dev.pub"


# --- default YAML location and content -----------------------------------------------------------------


def test_default_yaml_resolves_next_to_the_package_not_the_cwd(
    production: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Production runs from /opt/opengrid/current/integration-sims, where the old cwd-relative default
    ("integration-sims/config/fleet.yaml") did not exist, so the sims silently ran on built-in defaults."""
    production.chdir(tmp_path)
    loaded: list[str] = []
    real_load = sim_config.load_yaml_file

    def spy(path: str) -> dict:
        loaded.append(path)
        return real_load(path)

    production.setattr(sim_config, "load_yaml_file", spy)
    load_fleet_config()
    load_scada_config()

    assert loaded == [str(DEFAULT_FLEET_CONFIG_PATH), str(DEFAULT_SCADA_CONFIG_PATH)]
    assert DEFAULT_FLEET_CONFIG_PATH.is_file() and DEFAULT_SCADA_CONFIG_PATH.is_file()


def test_shipped_yaml_matches_the_built_in_defaults(production: pytest.MonkeyPatch) -> None:
    """Loading the shipped YAML instead of the built-in defaults changes nothing about the simulated
    fleet (39.2 kWh / 11 kW units, 20% dual-unit homes at 78.4 kWh / 20 kW, 600 kVA banks).

    `zone_blocks` is the one documented exception (build phase, 2026-09-26): `fleet.yaml` ships the
    Austin Energy/CPS Energy blocks pre-declared but `enabled: false`, which is asserted separately
    below to have zero effect on the simulated base fleet (`ZoneBlockConfig`'s docstring: a disabled
    block reserves no ids and changes nothing) -- it is excluded from the raw dict-equality check
    because it is the one field this shipped YAML deliberately does NOT match the bare
    "does-not-exist.yaml" built-in default (`()`, no blocks at all) on."""
    for loader, path in (
        (load_fleet_config, DEFAULT_FLEET_CONFIG_PATH),
        (load_scada_config, DEFAULT_SCADA_CONFIG_PATH),
    ):
        shipped = asdict(loader(str(path)))
        builtin = asdict(loader("does-not-exist.yaml"))
        shipped.pop("zone_blocks", None)
        builtin.pop("zone_blocks", None)
        shipped.pop("substation_assets", None)
        builtin.pop("substation_assets", None)
        assert shipped == builtin
    fleet = load_fleet_config()
    assert (fleet.e_kwh_default, fleet.p_kw_default, fleet.dual_unit_share) == (39.2, 11.0, 0.2)
    assert (fleet.e_kwh_dual_unit, fleet.p_kw_dual_unit, fleet.bank_kva_rating_default) == (78.4, 20.0, 600.0)
    assert (fleet.hub_count, fleet.bank_count) == (2000, 40)
    assert all(not block.enabled for block in fleet.zone_blocks)
    assert {block.zone for block in fleet.zone_blocks} == {"LZ_AEN", "LZ_CPS", "LZ_LCRA", "LZ_RAYBN"}
    assert [a.asset_id for a in fleet.substation_assets] == ["sub-LZ_AEN-00"]
    # OWNER DECISION D-29(b), 2026-09-26: the Austin substation asset is enabled for the utility toll
    # demo (its zone_blocks/LZ_AEN home-fleet block above stays disabled independently).
    assert fleet.substation_assets[0].enabled is True
    assert (fleet.substation_assets[0].rated_mw, fleet.substation_assets[0].duration_h) == (20.0, 2.0)


# --- broker credentials ----------------------------------------------------------------------


def test_workspace_credentials_win_for_the_fleet_sims(workspace: pytest.MonkeyPatch) -> None:
    workspace.setenv("OG_MQTT_ROOT", "ogtest/guardsafe")
    workspace.setenv("OG_MQTT_SIM_PASSWORD", "placeholder")
    settings = sim_config.mqtt_settings_from_env({})
    assert (settings.username, settings.password) == ("ogw_guardsafe", "ws-test-password")


def test_workspace_credentials_win_for_the_control_publisher(workspace: pytest.MonkeyPatch) -> None:
    from ogsim.control import mqtt_pub

    kwargs = mqtt_pub.mqtt_settings()
    assert (kwargs["username"], kwargs["password"]) == ("ogw_guardsafe", "ws-test-password")


def test_a_workspace_without_its_own_mqtt_user_is_refused(workspace: pytest.MonkeyPatch) -> None:
    workspace.delenv("OG_MQTT_WS_USER")
    workspace.setenv("OG_MQTT_ROOT", "ogtest/guardsafe")
    with pytest.raises(WorkspaceConfigError, match="OG_MQTT_WS_USER"):
        sim_config.mqtt_settings_from_env({})


def test_a_workspace_user_without_a_password_is_refused(workspace: pytest.MonkeyPatch) -> None:
    workspace.setenv("OG_MQTT_WS_PASSWORD", "")
    with pytest.raises(WorkspaceConfigError, match="OG_MQTT_WS_PASSWORD"):
        sim_config.resolve_mqtt_credentials("og_sim", "OG_MQTT_SIM_PASSWORD")


def test_production_uses_the_role_users(production: pytest.MonkeyPatch) -> None:
    production.setenv("OG_MQTT_SIM_PASSWORD", "prod-sim")
    production.setenv("OG_MQTT_SIMCTL_PASSWORD", "prod-simctl")
    from ogsim.control import mqtt_pub

    assert sim_config.resolve_mqtt_credentials("og_sim", "OG_MQTT_SIM_PASSWORD") == ("og_sim", "prod-sim")
    assert mqtt_pub.mqtt_settings()["username"] == "og_simctl"


def test_the_client_kwargs_carry_the_resolved_user() -> None:
    from ogsim.common.mqtt_client import mqtt_settings

    assert mqtt_settings("h", 1, "pw", "ogw_x")["username"] == "ogw_x"
    assert mqtt_settings("h", 1, "pw")["username"] == "og_sim"
