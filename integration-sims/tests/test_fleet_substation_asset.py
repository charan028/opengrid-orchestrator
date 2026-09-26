"""ogsim.fleet.state/_substation_segment: substation-sited battery-set assets
(09-optimizer-dispatcher-update.md D11 SUBSTATION_BESS, build phase 2026-09-26, FLEET-SIM
reassignment). Configurable, off by default, published/commanded/leased exactly like a home hub."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ogsim.common.config import FleetConfig, MqttSettings, SubstationAssetConfig
from ogsim.common.crypto import sign
from ogsim.fleet.runtime import FleetEngine
from ogsim.fleet.state import build_fleet_state

MQTT = MqttSettings(host="127.0.0.1", port=1883, username="og_sim", password="", topic_root="og/v1")


def test_disabled_substation_asset_is_not_simulated():
    config = FleetConfig(
        mqtt=MQTT,
        hub_count=2,
        bank_count=1,
        zones=("LZ_NORTH",),
        substation_assets=(SubstationAssetConfig(asset_id="sub-x", zone="LZ_AEN", enabled=False),),
    )
    state = build_fleet_state(config, np.random.default_rng(0))
    assert "sub-x" not in state.hub_ids
    assert len(state.hub_ids) == 2  # only the base fleet


def test_enabled_substation_asset_is_appended_with_its_own_bank():
    config = FleetConfig(
        mqtt=MQTT,
        hub_count=2,
        bank_count=1,
        zones=("LZ_NORTH",),
        substation_assets=(
            SubstationAssetConfig(
                asset_id="sub-LZ_AEN-00", zone="LZ_AEN", rated_mw=20.0, duration_h=2.0, enabled=True
            ),
        ),
    )
    state = build_fleet_state(config, np.random.default_rng(0))

    assert state.hub_ids[-1] == "sub-LZ_AEN-00"
    assert state.bank_ids[-1] == "bank-sub-LZ_AEN-00"
    assert state.zones[-1] == "LZ_AEN"
    idx = state.hub_index["sub-LZ_AEN-00"]
    assert state.p_kw_limit[idx] == pytest.approx(20_000.0)  # 20 MW in kW
    assert state.e_kwh[idx] == pytest.approx(40_000.0)  # 20 MW * 2 h in kWh
    assert state.r_kwh[idx] == pytest.approx(40_000.0 * 0.20)  # 20% reserve floor (D11 assumption)
    assert state.eta_c[idx] == pytest.approx(0.88**0.5)
    assert state.pv_capacity_kw[idx] == 0.0


def test_substation_asset_id_never_collides_with_home_hub_or_bank_ids():
    config = FleetConfig(
        mqtt=MQTT,
        hub_count=5,
        bank_count=1,
        zones=("LZ_NORTH",),
        substation_assets=(SubstationAssetConfig(asset_id="sub-a", zone="LZ_AEN", enabled=True),),
    )
    state = build_fleet_state(config, np.random.default_rng(0))
    home_hub_ids = set(state.hub_ids[:-1])
    home_bank_ids = set(state.bank_ids[:-1])
    assert "sub-a" not in home_hub_ids
    assert "bank-sub-a" not in home_bank_ids


def test_multiple_substation_assets_each_get_their_own_bank():
    config = FleetConfig(
        mqtt=MQTT,
        hub_count=1,
        bank_count=1,
        zones=("LZ_NORTH",),
        substation_assets=(
            SubstationAssetConfig(asset_id="sub-a", zone="LZ_AEN", enabled=True),
            SubstationAssetConfig(asset_id="sub-b", zone="LZ_CPS", enabled=True),
            SubstationAssetConfig(asset_id="sub-c", zone="LZ_HOUSTON", enabled=False),  # skipped
        ),
    )
    state = build_fleet_state(config, np.random.default_rng(0))
    assert state.hub_ids[-2:] == ["sub-a", "sub-b"]
    assert state.bank_ids[-2:] == ["bank-sub-a", "bank-sub-b"]
    assert state.zones[-2:] == ["LZ_AEN", "LZ_CPS"]
    assert "sub-c" not in state.hub_ids


def _engine_with_substation() -> FleetEngine:
    config = FleetConfig(
        mqtt=MQTT,
        hub_count=0,
        bank_count=1,
        zones=("LZ_NORTH",),
        substation_assets=(
            SubstationAssetConfig(
                asset_id="sub-LZ_AEN-00", zone="LZ_AEN", rated_mw=20.0, duration_h=2.0, enabled=True
            ),
        ),
    )
    return FleetEngine(config, seed=1)


def _signed_batch(guardian_key: Ed25519PrivateKey, hub_id: str, p_kw_setpoint: float) -> dict:
    now = datetime.now(UTC)
    batch = {
        "batch_id": str(uuid.uuid4()),
        "bank_id": f"bank-{hub_id}",
        "epoch": 1,
        "seq": 1,
        "issued_at": (now - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "expires_at": (now + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "items": [{"hub_id": hub_id, "p_kw_setpoint": p_kw_setpoint, "reason_code": "SELECTOR"}],
    }
    signing_fields = {
        k: batch[k] for k in ("batch_id", "bank_id", "epoch", "seq", "issued_at", "expires_at", "items")
    }
    batch["key_id"] = "guardian-test"
    batch["signature"] = sign(guardian_key, signing_fields)
    return batch


def test_substation_asset_accepts_signed_commands_and_publishes_telemetry_like_a_hub():
    engine = _engine_with_substation()
    hub_id = "sub-LZ_AEN-00"
    idx = engine.state.hub_index[hub_id]
    guardian_key = Ed25519PrivateKey.generate()
    engine.state.lease_expires_at[idx] = datetime.now(UTC).timestamp() + 3600.0  # held lease
    engine.state.soc_kwh[idx] = engine.state.e_kwh[idx]  # full, so discharge isn't SoC-limited

    batch = _signed_batch(guardian_key, hub_id, -15_000.0)  # -15 MW, within the 20 MW rating
    start = datetime.now(UTC).timestamp()
    verdicts = engine.handle_command_batch(batch, guardian_key.public_key(), now=start)
    assert verdicts[0].accepted is True

    engine.tick(now=start + 2.0)
    assert engine.state.p_kw_applied[idx] == pytest.approx(-15_000.0, rel=1e-3)

    messages = engine.telemetry_messages(now=start + 2.0)
    assert len(messages) == 1
    topic, msg = messages[0]
    assert topic == f"tel/LZ_AEN/bank-{hub_id}/{hub_id}"
    assert msg["hub_id"] == hub_id
    assert msg["p_kw"] == pytest.approx(-15_000.0, rel=1e-3)
