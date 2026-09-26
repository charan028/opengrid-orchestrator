"""ogsim.fleet.runtime -- fleet engine: ties physics/commands/lease/stop/
anomalies into one per-tick step, plus the thin async MQTT shell.

`FleetEngine` is pure (no I/O) and unit-testable with a `FakeClock`;
`run_fleet` is the async glue that feeds it from `ogsim.common.mqtt_client`.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import numpy as np
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ogsim.common.clock import Clock
from ogsim.common.config import FleetConfig
from ogsim.common.crypto import load_public_key
from ogsim.common.mqtt_client import SimMqttClient
from ogsim.common.scenario import ScenarioCommand, parse_scenario_cmd, utc_timestamp
from ogsim.fleet import household, physics
from ogsim.fleet.anomalies import FLEET_ANOMALY_TYPES, FleetAnomalyManager
from ogsim.fleet.calibration import CalibrationOutcome, apply_calibration, build_calibration_ack
from ogsim.fleet.commands import CommandVerdict, build_ack, evaluate_batch, utc_now_from_epoch
from ogsim.fleet.lease import HoldTracker, lease_expiry_from_message
from ogsim.fleet.pq import (
    PQ_ANOMALY_TYPES,
    InverterPqState,
    InverterSnapshot,
    PqAnomalyManager,
    build_inverter_pq_state,
    replace_inverter,
    tick_ambient_drift,
)
from ogsim.fleet.pq import inverter_state as pq_inverter_state
from ogsim.fleet.state import FleetState, build_fleet_state
from ogsim.fleet.stop import StopRegistry, ramp_toward_zero, verify_stop_event

logger = logging.getLogger(__name__)


class FleetEngine:
    """Pure per-tick fleet logic: home load, stop ramping, lease/autonomy,
    physics step, and anomaly bookkeeping. No MQTT, no wall-clock sleeps."""

    def __init__(self, config: FleetConfig, seed: int = 0) -> None:
        self.config = config
        self.rng = np.random.default_rng(seed)
        self.state: FleetState = build_fleet_state(config, self.rng)
        self.anomalies = FleetAnomalyManager(self.state)
        self.pq: InverterPqState = build_inverter_pq_state(config, self.state, self.rng)
        self.pq_anomalies = PqAnomalyManager(self.pq)
        self.stops = StopRegistry()
        self.holds = HoldTracker(config.lease_hold_after_expiry_s)
        self._last_tick_at: float | None = None

    def handle_scenario_cmd(self, raw: dict[str, Any]) -> ActiveAnomalyStarted | None:
        cmd = parse_scenario_cmd(raw)
        if cmd.catalogue_type == "replace_inverter":
            replace_inverter(
                self.pq,
                self.pq_anomalies,
                cmd.target_ref,
                str(cmd.params.get("new_serial", "")),
                str(cmd.params.get("new_firmware", "")),
                self.rng,
            )
            return ActiveAnomalyStarted(cmd)
        if cmd.catalogue_type in PQ_ANOMALY_TYPES:
            self.pq_anomalies.start(
                cmd.id,
                cmd.catalogue_type,
                cmd.target_kind,
                cmd.target_ref,
                cmd.params,
                cmd.start_epoch,
                cmd.duration_s,
            )
            return ActiveAnomalyStarted(cmd)
        if cmd.catalogue_type not in FLEET_ANOMALY_TYPES:
            return None
        self.anomalies.start(
            cmd.id,
            cmd.catalogue_type,
            cmd.target_kind,
            cmd.target_ref,
            cmd.params,
            cmd.start_epoch,
            cmd.duration_s,
        )
        return ActiveAnomalyStarted(cmd)

    def handle_calibration_command(
        self, command: dict[str, Any], public_key: Ed25519PublicKey, now: float
    ) -> dict[str, Any]:
        """Verifies and applies a `CalibrationCommand` (§6.7) against
        `ogsim.fleet.pq`, returning the `CalibrationAck`-shaped message."""
        outcome: CalibrationOutcome = apply_calibration(
            self.pq, self.pq_anomalies, command, public_key, now, self.config.pq_calibration_rate_limit_s
        )
        return build_calibration_ack(outcome, utc_timestamp(now))

    def inverter_state(self, hub_id: str) -> list[InverterSnapshot]:
        """Clean, WP-H-facing accessor (§7.4): the per-unit parameters needed
        to synthesize a waveform for `hub_id`. This engine never generates
        samples itself."""
        return pq_inverter_state(self.pq, hub_id)

    def handle_stop_event(
        self,
        event: dict[str, Any],
        safestop_public_key: Ed25519PublicKey,
        guardian_public_key: Ed25519PublicKey,
    ) -> bool:
        """Verifies `event` per crypto.md §2.3 (`verify_stop_event`) and
        only then applies it to `self.stops`. A rejected event -- wrong key
        for the action, or a bad/missing signature -- is logged and never
        reaches `StopRegistry`. Returns True if applied."""
        reject_reason = verify_stop_event(event, safestop_public_key, guardian_public_key)
        if reject_reason is not None:
            logger.warning(
                "rejected stop event: reason=%s stop_id=%s action=%s scope=%s scope_id=%s key_id=%s",
                reject_reason,
                event.get("stop_id"),
                event.get("action"),
                event.get("scope"),
                event.get("scope_id"),
                event.get("key_id"),
            )
            return False
        self.stops.apply_stop_event(
            str(event.get("action", "")), str(event.get("scope", "")), event.get("scope_id")
        )
        return True

    def handle_lease_message(self, hub_id: str, expires_at: str) -> None:
        idx = self.state.hub_index.get(hub_id)
        if idx is None:
            return
        self.state.lease_expires_at[idx] = lease_expiry_from_message(expires_at)
        self.holds.renew(hub_id)

    def handle_command_batch(
        self, batch: dict[str, Any], public_key: Ed25519PublicKey, now: float
    ) -> list[CommandVerdict]:
        last_accepted = {
            hub_id: (int(self.state.last_epoch[i]), int(self.state.last_seq[i]))
            for hub_id, i in self.state.hub_index.items()
        }
        verdicts = evaluate_batch(batch, public_key, last_accepted, utc_now_from_epoch(now))
        epoch = int(batch.get("epoch", -1))
        seq = int(batch.get("seq", -1))
        lease = batch.get("lease")
        for verdict in verdicts:
            idx = self.state.hub_index.get(verdict.hub_id)
            if idx is None or not verdict.accepted or verdict.requested_p_kw_setpoint is None:
                continue
            self.state.last_epoch[idx] = epoch
            self.state.last_seq[idx] = seq
            self.state.p_kw_commanded[idx] = verdict.requested_p_kw_setpoint
            if lease and isinstance(lease, dict) and "expires_at" in lease:
                self.handle_lease_message(verdict.hub_id, lease["expires_at"])
        return verdicts

    def tick(self, now: float) -> None:
        dt_s = self.config.telemetry_interval_s if self._last_tick_at is None else now - self._last_tick_at
        self._last_tick_at = now
        self.anomalies.tick(now)
        self.anomalies.accumulate_drift(dt_s)
        self.pq_anomalies.tick(now)
        tick_ambient_drift(self.pq, dt_s, self.rng)

        state = self.state
        home_net = household.net_home_load_kw(now, state.phase_offset_s, state.pv_capacity_kw, self.rng)
        forced = self.anomalies.modifiers.forced_home_load_kw
        home_net = np.where(np.isnan(forced), home_net, forced)

        effective_commanded = state.p_kw_commanded * self.anomalies.modifiers.follow_fraction
        effective_commanded = np.where(self.anomalies.modifiers.inverter_tripped, 0.0, effective_commanded)
        effective_commanded = effective_commanded + self._pq_dispatch_bias_by_hub()

        for i, (zone, bank_id) in enumerate(zip(state.zones, state.bank_ids, strict=True)):
            hub_id = state.hub_ids[i]
            hold_state = self.holds.state_for(hub_id, float(state.lease_expires_at[i]), now)
            state.local_autonomy[i] = hold_state.local_autonomy
            state.holding_after_expiry[i] = hold_state.holding
            if hold_state.local_autonomy:
                effective_commanded[i] = 0.0
            if self.stops.is_stopped(zone, bank_id):
                effective_commanded[i] = ramp_toward_zero(
                    float(effective_commanded[i]), dt_s, self.config.stop_ramp_s, float(state.p_kw_limit[i])
                )
            if i in self.anomalies.modifiers.force_lease_expire:
                state.lease_expires_at[i] = now - 1.0
                self.anomalies.modifiers.force_lease_expire.discard(i)

        new_soc, applied = physics.tick(
            state.soc_kwh,
            effective_commanded,
            home_net,
            state.p_kw_limit,
            state.e_kwh,
            state.r_kwh,
            state.eta_c,
            state.eta_d,
            dt_s,
            state.self_discharge_kwh_per_h,
        )
        state.soc_kwh = new_soc
        state.p_kw_applied = applied

    def _pq_dispatch_bias_by_hub(self) -> np.ndarray:
        """Aggregates `phase_imbalance_injection`'s per-unit `dispatch_bias_kw`
        (§7.2) onto `self.state`'s per-hub index, summing a dual-unit home's
        two units' biases onto its one hub row."""
        bias_kw = np.zeros(len(self.state.hub_ids))
        pq_bias = self.pq_anomalies.dispatch_bias_kw
        if not np.any(pq_bias != 0.0):
            return bias_kw
        for i, hub_id in enumerate(self.pq.hub_ids):
            if pq_bias[i] == 0.0:
                continue
            idx = self.state.hub_index.get(hub_id)
            if idx is not None:
                bias_kw[idx] += pq_bias[i]
        return bias_kw

    def telemetry_messages(self, now: float) -> list[tuple[str, dict[str, Any]]]:
        """Builds `(topic_suffix, message)` pairs for every hub not currently
        suppressed by an anomaly, applying reported-SoC drift and clock skew."""
        state = self.state
        m = self.anomalies.modifiers
        reported_soc = np.clip(state.soc_kwh + m.soc_drift_kwh, 0.0, None)
        messages: list[tuple[str, dict[str, Any]]] = []
        for i in range(len(state.hub_ids)):
            if m.telemetry_suppressed[i]:
                continue
            ts = utc_timestamp(now + float(m.clock_skew_s[i]))
            messages.append(
                (
                    f"tel/{state.zones[i]}/{state.bank_ids[i]}/{state.hub_ids[i]}",
                    {
                        "hub_id": state.hub_ids[i],
                        "bank_id": state.bank_ids[i],
                        "zone": state.zones[i],
                        "ts": ts,
                        "soc_kwh": round(float(reported_soc[i]), 4),
                        "p_kw": round(float(state.p_kw_applied[i]), 4),
                        "health": state.health[i],
                        "seq": int(state.last_seq[i]) if state.last_seq[i] >= 0 else 0,
                        "epoch": int(state.last_epoch[i]) if state.last_epoch[i] >= 0 else 0,
                        "fault_code": state.fault_code[i],
                    },
                )
            )
        return messages

    def build_ack(self, verdict: CommandVerdict, batch_id: str, now: float) -> dict[str, Any]:
        """Builds this hub's `ack.schema.json` message via the pure
        `ogsim.fleet.commands.build_ack`, supplying the hub's actual
        post-physics applied power (after the SoC/reserve clamp)."""
        return build_ack(verdict, batch_id, self._applied_p_kw(verdict), now)

    def _applied_p_kw(self, verdict: CommandVerdict) -> float | None:
        if not verdict.accepted:
            return None
        idx = self.state.hub_index.get(verdict.hub_id)
        return None if idx is None else round(float(self.state.p_kw_applied[idx]), 4)

    def self_test_tampered_unsigned_command(
        self, bank_id: str, public_key: Ed25519PublicKey
    ) -> CommandVerdict:
        """Fleet's own tampered_unsigned_command self-test (BUILD.md point 4):
        builds a forged, unsigned batch for one hub on `bank_id` and confirms
        the normal verification path rejects it with BAD_SIGNATURE. Never
        applied to real state."""
        hub_id = next(
            (h for h, b in zip(self.state.hub_ids, self.state.bank_ids, strict=True) if b == bank_id), None
        )
        if hub_id is None:
            raise ValueError(f"no hub on bank {bank_id!r}")
        forged = {
            "batch_id": str(uuid.uuid4()),
            "bank_id": bank_id,
            "epoch": 999999,
            "seq": 1,
            "issued_at": utc_timestamp(0.0),
            # A fixed far-future literal, not utc_timestamp(huge_epoch):
            # datetime.fromtimestamp() raises OSError on Windows for
            # timestamps outside its platform-native range.
            "expires_at": "2999-01-01T00:00:00.000Z",
            "items": [{"hub_id": hub_id, "p_kw_setpoint": -5.0, "reason_code": "FORGED_SELFTEST"}],
            "key_id": "forged-key",
            "signature": "",
        }
        verdicts = evaluate_batch(forged, public_key, {}, utc_now_from_epoch(0.0))
        verdict = verdicts[0]
        if verdict.accepted:
            raise AssertionError("tampered_unsigned_command self-test FAILED: forged command was accepted")
        logger.warning("fleet self-test: forged command correctly rejected (%s)", verdict.reject_reason)
        return verdict


class ActiveAnomalyStarted:
    def __init__(self, cmd: ScenarioCommand) -> None:
        self.cmd = cmd


def load_guardian_public_key(config: FleetConfig) -> Ed25519PublicKey:
    with open(config.public_key_path(), encoding="utf-8") as fh:
        return load_public_key(fh.read())


def load_safestop_public_key(config: FleetConfig) -> Ed25519PublicKey:
    with open(config.safestop_key_path(), encoding="utf-8") as fh:
        return load_public_key(fh.read())


async def run_fleet(
    client: SimMqttClient, engine: FleetEngine, clock: Clock, public_key: Ed25519PublicKey
) -> None:
    """Async shell: subscribes to inbound topics, ticks `engine` on
    `config.telemetry_interval_s`, and publishes telemetry. Intended to run
    under `asyncio.gather` alongside a message-consuming task (which
    publishes acks as command batches arrive).

    One tick's body (physics step + publish) is wrapped in its own
    try/except: a single bad tick -- a transient publish failure, a
    momentarily unreachable broker, anything that would otherwise raise out
    of this `while True` loop -- must never silently end telemetry for the
    rest of the process's life (qa/merge-notes.md section 12: og-sim-fleet
    going idle after one burst per restart, with nothing logged and no
    crash/restart to explain why). The error is logged and the loop keeps
    ticking on schedule; a hub simply misses one telemetry publish rather
    than every hub going stale forever."""
    await client.subscribe("cmd/+/batch", qos=1)
    await client.subscribe("stop/#", qos=1)
    await client.subscribe("lease/+", qos=1)
    await client.subscribe("scenario/cmd", qos=1)
    while True:
        now = clock.now()
        try:
            engine.tick(now)
            await client.publish_batch("telemetry", engine.telemetry_messages(now), qos=0)
        except Exception:
            logger.exception("fleet tick failed; continuing telemetry loop")
        await clock.sleep(engine.config.telemetry_interval_s)
