from datetime import UTC, datetime

import pytest

from opengrid.platform import mqtt
from opengrid.platform.config import load_config
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
