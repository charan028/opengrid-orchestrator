from datetime import UTC, datetime

import pytest

from opengrid.platform import mqtt
from opengrid.platform.config import Config, load_config
from opengrid.platform.mqtt import SchemaValidationError, topic, validate_payload


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    # BUILD.md S5: strip workspace overrides so this test is deterministic on dev and server alike.
    monkeypatch.delenv("OG_MQTT_ROOT", raising=False)
    path = tmp_path / "orchestrator.toml"
    path.write_text('[mqtt]\ntopic_root = "og/v1"\n', encoding="utf-8")
    return load_config(path)


def test_topic_prefixes_with_configured_root(cfg):
    assert topic(cfg, "tel/+/+/+") == "og/v1/tel/+/+/+"


def test_validate_payload_accepts_valid_telemetry():
    payload = {
        "hub_id": "hub-1",
        "bank_id": "bank-1",
        "zone": "LZ_NORTH",
        "ts": datetime.now(UTC).isoformat(),
        "soc_kwh": 5.0,
        "p_kw": -1.5,
        "health": "online",
        "seq": 1,
        "epoch": 1,
    }
    validate_payload("telemetry", payload)  # should not raise


def test_validate_payload_rejects_missing_field():
    payload = {"hub_id": "hub-1"}
    with pytest.raises(SchemaValidationError):
        validate_payload("telemetry", payload)


def test_validate_payload_rejects_bad_enum():
    payload = {
        "hub_id": "hub-1",
        "bank_id": "bank-1",
        "zone": "LZ_NORTH",
        "ts": datetime.now(UTC).isoformat(),
        "soc_kwh": 5.0,
        "p_kw": -1.5,
        "health": "not-a-valid-state",
        "seq": 1,
        "epoch": 1,
    }
    with pytest.raises(SchemaValidationError):
        validate_payload("telemetry", payload)


def test_validate_payload_reuses_one_compiled_validator_per_kind():
    """Regression: `validate_payload` must not re-verify the schema itself (`jsonschema.validate`'s
    `check_schema`) on every call -- at MQTT ingest volume (~1,000 msg/s for 2,000 hubs) that made
    `og-engine`'s single-threaded ingest loop fall permanently behind wall-clock time (confirmed live
    via `py-spy dump`), which is the actual mechanism behind qa/merge-notes.md section 12's "og-sim-fleet
    goes idle" symptom. `_validator_for` is `@cache`d, so the same `Validator` instance must come back
    for repeated calls with the same `kind`."""
    payload = {
        "hub_id": "hub-1",
        "bank_id": "bank-1",
        "zone": "LZ_NORTH",
        "ts": datetime.now(UTC).isoformat(),
        "soc_kwh": 5.0,
        "p_kw": -1.5,
        "health": "online",
        "seq": 1,
        "epoch": 1,
    }
    validate_payload("telemetry", payload)
    validate_payload("telemetry", payload)

    first = mqtt._validator_for("telemetry")
    second = mqtt._validator_for("telemetry")
    assert first is second


def test_validate_payload_command_batch():
    payload = {
        "batch_id": "0190f7b0-0000-7000-8000-000000000000",
        "bank_id": "bank-07",
        "epoch": 1,
        "seq": 1,
        "issued_at": datetime.now(UTC).isoformat(),
        "expires_at": datetime.now(UTC).isoformat(),
        "items": [{"hub_id": "hub-1", "p_kw_setpoint": -3.0, "reason_code": "SELECTOR"}],
        "key_id": "guardian-2026a",
        "signature": "abc",
    }
    validate_payload("command_batch", payload)


# --- MQTT client identity (a workspace must never reuse a production client id) ------------------------


def _identity_cfg(*, prefix: str | None = None, env: str | None = None) -> Config:
    data: dict = {"mqtt": {"topic_root": "og/v1"}}
    if prefix is not None:
        data["mqtt"]["client_id_prefix"] = prefix
    if env is not None:
        data["general"] = {"env": env}
    return Config(data)


def test_production_client_ids_are_unchanged(monkeypatch):
    monkeypatch.delenv("OG_WS", raising=False)
    cfg = _identity_cfg(prefix="og", env="prod")
    assert mqtt.compose_client_id(cfg, "guardian") == "og-guardian"
    assert mqtt.compose_client_id(cfg, "guardian-tel") == "og-guardian-tel"
    assert mqtt.compose_client_id(cfg, "safestop") == "og-safestop"


def test_workspace_client_ids_carry_the_prefix_and_workspace(monkeypatch):
    monkeypatch.setenv("OG_WS", "guardsafe")
    cfg = _identity_cfg(prefix="og-test", env="dev")
    assert mqtt.compose_client_id(cfg, "engine") == "og-test-guardsafe-engine"


@pytest.mark.parametrize(("workspace", "env"), [("guardsafe", "prod"), ("", "dev"), ("guardsafe", "dev")])
def test_production_prefix_is_refused_outside_production(monkeypatch, workspace, env):
    """The broker drops the older session on a duplicate client id: a workspace or dev run with the
    production prefix would silently disconnect og-guardian/og-engine/og-safestop in production."""
    monkeypatch.setenv("OG_WS", workspace)
    with pytest.raises(mqtt.MqttIdentityError):
        mqtt.compose_client_id(_identity_cfg(prefix="og", env=env), "guardian")


def test_missing_prefix_defaults_to_production_and_is_refused_in_a_workspace(monkeypatch):
    monkeypatch.setenv("OG_WS", "guardsafe")
    with pytest.raises(mqtt.MqttIdentityError):
        mqtt.compose_client_id(_identity_cfg(env="dev"), "guardian")


def test_build_client_composes_the_identifier(monkeypatch):
    monkeypatch.setenv("OG_WS", "guardsafe")
    monkeypatch.setenv("OG_MQTT_WS_USER", "ogw_guardsafe")
    monkeypatch.setenv("OG_MQTT_WS_PASSWORD", "ws-test-password")
    captured: dict = {}

    def fake_client(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(mqtt.aiomqtt, "Client", fake_client)
    cfg = _identity_cfg(prefix="og-test", env="dev")

    mqtt.build_client(cfg, username="u", password="p", process="guardian")
    assert captured["identifier"] == "og-test-guardsafe-guardian"

    mqtt.build_client(cfg, username="u", password="p", client_id="og-engine")  # legacy call sites
    assert captured["identifier"] == "og-test-guardsafe-engine"


def test_build_client_refuses_a_workspace_with_the_production_prefix(monkeypatch):
    monkeypatch.setenv("OG_WS", "guardsafe")
    monkeypatch.setattr(mqtt.aiomqtt, "Client", lambda **kwargs: object())
    with pytest.raises(mqtt.MqttIdentityError):
        mqtt.build_client(
            _identity_cfg(prefix="og", env="prod"), username="u", password="p", process="guardian"
        )


def test_calibration_schemas_are_registered():
    assert "calibration_command" in mqtt._SCHEMA_BY_KIND and "calibration_ack" in mqtt._SCHEMA_BY_KIND


# --- MQTT credentials: workspace env wins; a workspace never falls back to production users ------------------


def _no_ws_creds(monkeypatch):
    for name in ("OG_WS", "OG_MQTT_WS_USER", "OG_MQTT_WS_PASSWORD"):
        monkeypatch.delenv(name, raising=False)


def test_production_uses_the_callers_role_credentials(monkeypatch):
    _no_ws_creds(monkeypatch)
    assert mqtt.resolve_mqtt_credentials("og_guardian", "role-pw") == ("og_guardian", "role-pw")


def test_workspace_credentials_win_over_the_role_user(monkeypatch):
    _no_ws_creds(monkeypatch)
    monkeypatch.setenv("OG_WS", "guard")
    monkeypatch.setenv("OG_MQTT_WS_USER", "ogw_guard")
    monkeypatch.setenv("OG_MQTT_WS_PASSWORD", "ws-pw")
    assert mqtt.resolve_mqtt_credentials("og_guardian", "placeholder") == ("ogw_guard", "ws-pw")


def test_a_workspace_without_its_own_user_is_refused(monkeypatch):
    _no_ws_creds(monkeypatch)
    monkeypatch.setenv("OG_WS", "guard")
    with pytest.raises(mqtt.MqttIdentityError, match="OG_MQTT_WS_USER"):
        mqtt.resolve_mqtt_credentials("og_guardian", "real-production-password")


def test_a_workspace_user_without_a_password_is_refused(monkeypatch):
    _no_ws_creds(monkeypatch)
    monkeypatch.setenv("OG_MQTT_WS_USER", "ogw_guard")
    with pytest.raises(mqtt.MqttIdentityError, match="OG_MQTT_WS_PASSWORD"):
        mqtt.resolve_mqtt_credentials("og_guardian", "x")


def test_build_client_connects_as_the_workspace_user(monkeypatch):
    _no_ws_creds(monkeypatch)
    monkeypatch.setenv("OG_WS", "guard")
    monkeypatch.setenv("OG_MQTT_WS_USER", "ogw_guard")
    monkeypatch.setenv("OG_MQTT_WS_PASSWORD", "ws-pw")
    captured: dict = {}
    monkeypatch.setattr(mqtt.aiomqtt, "Client", lambda **kwargs: captured.update(kwargs))

    mqtt.build_client(
        _identity_cfg(prefix="og-test", env="dev"),
        username="og_engine",
        password="placeholder",
        process="engine",
    )

    assert (captured["username"], captured["password"]) == ("ogw_guard", "ws-pw")
