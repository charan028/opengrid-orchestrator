"""ogsim.fleet.runtime.FleetEngine.handle_command_batch: live bug fix (2026-09-26) -- a hub serving
two obligations/grants in the same batch previously delivered only the LAST item's setpoint (each
verdict overwrote `p_kw_commanded` in turn instead of summing). All verified items for one hub in one
batch must now sum into a single commanded setpoint, still clipped by the hub's own physics (rated
`p_kw`, SoC/reserve floor) exactly as before."""

from __future__ import annotations

import uuid
from dataclasses import replace as dc_replace
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ogsim.common.config import MqttSettings, load_fleet_config
from ogsim.common.crypto import sign
from ogsim.fleet.runtime import FleetEngine

MQTT = MqttSettings(host="127.0.0.1", port=1883, username="og_sim", password="", topic_root="og/v1")


def _engine(**overrides: object) -> FleetEngine:
    config = dc_replace(
        load_fleet_config(), mqtt=MQTT, hub_count=1, bank_count=1, zones=("LZ_NORTH",), **overrides
    )
    return FleetEngine(config, seed=1)


def _signed_batch(
    guardian_key: Ed25519PrivateKey, items: list[dict], *, epoch: int = 1, seq: int = 1
) -> dict:
    now = datetime.now(UTC)
    batch = {
        "batch_id": str(uuid.uuid4()),
        "bank_id": "bank-000",
        "epoch": epoch,
        "seq": seq,
        "issued_at": (now - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "expires_at": (now + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "items": items,
    }
    signing_fields = {
        k: batch[k] for k in ("batch_id", "bank_id", "epoch", "seq", "issued_at", "expires_at", "items")
    }
    batch["key_id"] = "guardian-test"
    batch["signature"] = sign(guardian_key, signing_fields)
    return batch


def test_two_items_on_one_hub_sum_into_one_commanded_setpoint() -> None:
    engine = _engine()
    guardian_key = Ed25519PrivateKey.generate()
    hub_id = engine.state.hub_ids[0]
    # Two obligations dispatching the same hub in one batch, well under its rated 11 kW so no
    # clipping should mask the bug: -3.0 + -2.0 must sum to -5.0, not overwrite to -2.0.
    items = [
        {"hub_id": hub_id, "p_kw_setpoint": -3.0, "reason_code": "OBLIGATION_A"},
        {"hub_id": hub_id, "p_kw_setpoint": -2.0, "reason_code": "OBLIGATION_B"},
    ]
    batch = _signed_batch(guardian_key, items)

    verdicts = engine.handle_command_batch(
        batch, guardian_key.public_key(), now=datetime.now(UTC).timestamp()
    )

    assert len(verdicts) == 2
    assert all(v.accepted for v in verdicts)
    idx = engine.state.hub_index[hub_id]
    assert engine.state.p_kw_commanded[idx] == -5.0


def test_summed_setpoint_is_still_clipped_by_hub_physics() -> None:
    """Two items requesting -8.0 and -8.0 kW (sum -16.0) on an 11.0 kW-rated hub: the sum must still
    be clipped to the hub's physical/SoC-available discharge ceiling by `tick`'s physics step, exactly
    as a single over-limit item would be -- the fix only changes how the pre-clip setpoint is built."""
    engine = _engine()
    guardian_key = Ed25519PrivateKey.generate()
    hub_id = engine.state.hub_ids[0]
    idx = engine.state.hub_index[hub_id]
    engine.state.soc_kwh[idx] = engine.state.e_kwh[idx]  # full SoC: plenty of energy to discharge

    items = [
        {"hub_id": hub_id, "p_kw_setpoint": -8.0, "reason_code": "OBLIGATION_A"},
        {"hub_id": hub_id, "p_kw_setpoint": -8.0, "reason_code": "OBLIGATION_B"},
    ]
    batch = _signed_batch(guardian_key, items)
    start = datetime.now(UTC).timestamp()
    engine.handle_command_batch(batch, guardian_key.public_key(), now=start)
    assert engine.state.p_kw_commanded[idx] == -16.0  # summed, pre-clip
    engine.state.lease_expires_at[idx] = start + 3600.0  # held lease, so the setpoint isn't overridden
    # to 0 by local-autonomy hold (a separate concern from this fix -- ogsim.fleet.lease).

    engine.tick(now=start + 2.0)

    assert engine.state.p_kw_applied[idx] >= -11.0  # clipped to the hub's rated power, not -16.0
    assert engine.state.p_kw_applied[idx] < -8.0  # clipping still allows more than either item alone


def test_rejected_item_is_excluded_from_the_sum() -> None:
    """A stale/rejected item on a hub must not contribute to that hub's summed setpoint, even when
    another item for the same hub in the same batch is accepted."""
    engine = _engine()
    guardian_key = Ed25519PrivateKey.generate()
    hub_id = engine.state.hub_ids[0]
    idx = engine.state.hub_index[hub_id]
    engine.state.last_epoch[idx] = 5  # ahead of the batch below -> STALE_EPOCH for any item on it

    items = [{"hub_id": hub_id, "p_kw_setpoint": -3.0, "reason_code": "OBLIGATION_A"}]
    batch = _signed_batch(guardian_key, items, epoch=1, seq=1)

    verdicts = engine.handle_command_batch(batch, guardian_key.public_key(), now=0.0)

    assert verdicts[0].accepted is False
    assert verdicts[0].reject_reason == "STALE_EPOCH"
    assert engine.state.p_kw_commanded[idx] == 0.0  # unchanged from its initial value
