"""Cross-package K8 contract: the hub's own code must accept the stop RELEASE the orchestrator's guardian
signed after Tier-2 approval, pinned in `interfaces/fixtures/signed_stop_release.json` (written by
`orchestrator/tests/unit/guardian/test_signed_stop_release_fixture.py`), and must lift the stop with it."""

from __future__ import annotations

import copy
import json
from dataclasses import replace as dc_replace
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ogsim.common.config import MqttSettings, load_fleet_config
from ogsim.common.crypto import load_public_key
from ogsim.fleet.__main__ import handle_stop_message
from ogsim.fleet.runtime import FleetEngine
from ogsim.fleet.stop import verify_stop_event

FIXTURE = Path(__file__).resolve().parents[2] / "interfaces" / "fixtures" / "signed_stop_release.json"
MQTT = MqttSettings(host="127.0.0.1", port=1883, username="og_sim", password="", topic_root="ogtest/unit")


def _load() -> tuple[dict, object]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data["release"], load_public_key(data["guardian_public_key_hex"])


def test_hub_verifies_the_guardian_signed_release() -> None:
    release, guardian_key = _load()
    safestop_key = Ed25519PrivateKey.generate().public_key()
    assert verify_stop_event(release, safestop_key, guardian_key) is None  # type: ignore[arg-type]


def test_hub_lifts_the_stop_with_it_and_rejects_any_alteration() -> None:
    release, guardian_key = _load()
    safestop_key = Ed25519PrivateKey.generate().public_key()
    config = dc_replace(load_fleet_config(), mqtt=MQTT, hub_count=4, bank_count=1, zones=("LZ_NORTH",))
    engine = FleetEngine(config, seed=1)
    engine.stops.engage("bank", "bank-000")

    tampered = copy.deepcopy(release)
    tampered["approver_ref"] = "operator:mallory"
    topic = f"ogtest/unit/stop/bank/bank-000/{release['stop_id']}"
    assert not handle_stop_message(engine, topic, tampered, safestop_key, guardian_key)
    assert "bank-000" in engine.stops.banks_stopped

    assert handle_stop_message(engine, topic, release, safestop_key, guardian_key)
    assert "bank-000" not in engine.stops.banks_stopped
