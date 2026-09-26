"""MQTT I/O for `og-guardian` (02b S6.1-6.2, `interfaces/mqtt/topics.md`).

Two directions:
1. **Independent reads** (BUILD.md: "It reads its own hub telemetry, subscribe `<root>/tel/#`").
   `MqttHubStatePort` is guardian's OWN telemetry cache, filled by `run_telemetry_listener` -- it never
   reads `og.hub_state` (the row `opengrid.fleet`/`engine` maintain), because K1/K4's guarantee depends
   on guardian checking a value nobody else could have altered on the way in. The same listener feeds
   `MqttL2InstructionPort` from `<root>/scada/instruction/<bank_id>` (K5/G-15 and G-19's L2 override
   check), again the guardian's own subscription, never the engine's copy.
2. **Signed publish**: `publish_command_batch` (`<root>/cmd/<bank_id>/batch`, QoS 1),
   `publish_calibration_command` (`<root>/cmd/cal/<hub_id>`, QoS 1) and `publish_lease`
   (`<root>/lease/<hub_id>`, QoS 1, retained) -- guardian is the only publisher of all three.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

import aiomqtt

from opengrid.core.models.mqtt import CommandBatch, Lease, ScadaUtilityInstruction, Telemetry
from opengrid.core.models.pq import CalibrationCommand
from opengrid.guardian.ports import HubFlowTelemetry, HubSnapshot, L2Instruction, Reading
from opengrid.platform.config import Config
from opengrid.platform.mqtt import topic, validate_payload

logger = logging.getLogger(__name__)

#: Hub telemetry fields the flow-limit checks read (09 S2.6; `HubFlowTelemetry`).
FLOW_TELEMETRY_FIELDS = ("meter_kw", "pv_kw", "cell_temp_c", "p_dis_max_kw", "p_ch_max_kw", "peak_budget_kws")


class MqttHubStatePort:
    """Guardian's own hub-state view, built exclusively from telemetry it has itself received over
    MQTT plus each hub's static physical parameters (capacity/reserve/power limit are configuration,
    not a live signal, so reading them from `og.hub` does not compromise independence).

    K1: a hub whose telemetry the guardian itself has not received within `max_age_s` is reported
    `stale` whatever its last message said, so a silent hub never keeps discharging on an old SoC."""

    def __init__(
        self,
        hub_params_by_id: dict[str, HubSnapshot],
        *,
        bank_by_hub: dict[str, str] | None = None,
        max_age_s: float | None = None,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        # hub_params_by_id is seeded with soc/p_kw placeholders; ingest() overwrites them per message.
        self._snapshots = dict(hub_params_by_id)
        self._received_at: dict[str, float] = {}
        self._max_age_s = max_age_s
        self._monotonic = monotonic_fn
        self._flow: dict[str, dict[str, tuple[float, float]]] = {}
        self._hubs_by_bank: dict[str, list[str]] = {}
        for hub_id, bank_id in (bank_by_hub or {}).items():
            self._hubs_by_bank.setdefault(bank_id, []).append(hub_id)

    def ingest(self, message: Telemetry) -> None:
        prior = self._snapshots.get(message.hub_id)
        if prior is None:
            return  # unknown hub (not in og.hub) -- G-01/G-02 will fail closed as HUB_UNKNOWN downstream
        self._snapshots[message.hub_id] = HubSnapshot(
            params=prior.params, soc_kwh=message.soc_kwh, prev_p_kw=message.p_kw, health=message.health
        )
        now = self._monotonic()
        self._received_at[message.hub_id] = now
        flow = self._flow.setdefault(message.hub_id, {})
        for name in FLOW_TELEMETRY_FIELDS:
            value = getattr(message, name, None)  # additive telemetry fields (09 S2.6) as FLEET-SIM adds them
            if isinstance(value, int | float) and math.isfinite(float(value)):
                flow[name] = (float(value), now)

    def _flow_telemetry(self, hub_id: str) -> HubFlowTelemetry:
        now = self._monotonic()
        readings = {
            name: Reading(value, now - received)
            for name, (value, received) in self._flow.get(hub_id, {}).items()
        }
        return HubFlowTelemetry(**readings)

    async def snapshot(self, hub_id: str) -> HubSnapshot | None:
        """Any snapshot the guardian has not refreshed within `max_age_s` is `stale`, whatever its last
        message said -- an old "offline"/"fault" report is as unverifiable now as an old "online"."""
        snap = self._snapshots.get(hub_id)
        if snap is not None and hub_id in self._flow:
            snap = replace(snap, flow=self._flow_telemetry(hub_id))
        if snap is None or self._max_age_s is None or snap.health == "stale":
            return snap
        received_at = self._received_at.get(hub_id)
        if received_at is None or self._monotonic() - received_at > self._max_age_s:
            return replace(snap, health="stale")
        return snap

    async def member_snapshots(self, bank_id: str) -> list[HubSnapshot]:
        """`BankMembersPort`: the guardian's own snapshot of every configured hub on `bank_id`."""
        snapshots = [await self.snapshot(hub_id) for hub_id in self._hubs_by_bank.get(bank_id, [])]
        return [s for s in snapshots if s is not None]

    async def member_hub_ids(self, bank_id: str) -> list[str]:
        """`BankMembersPort`: the configured hub ids on `bank_id` (og.hub membership)."""
        return list(self._hubs_by_bank.get(bank_id, []))


class MqttL2InstructionPort:
    """K5: the latest utility/ISO instruction per bank, from the guardian's own subscription to
    `<root>/scada/instruction/<bank_id>`. An instruction past its `expires_at` is no longer active.
    After a restart the cache is empty until the next instruction arrives: G-15 then has nothing to
    enforce (the allocator's primary check still does), and a G-19 L2 override is not corroborated, so it
    is vetoed -- a hold, never an unverified commitment reduction."""

    def __init__(self, now_fn: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._latest: dict[str, ScadaUtilityInstruction] = {}
        self._now = now_fn

    def ingest(self, instruction: ScadaUtilityInstruction) -> None:
        self._latest[instruction.bank_id] = instruction

    async def active_instruction(self, bank_id: str) -> L2Instruction | None:
        instruction = self._latest.get(bank_id)
        if instruction is None:
            return None
        if instruction.expires_at is not None and instruction.expires_at <= self._now():
            return None
        return L2Instruction(kind=instruction.kind, limit_kw=instruction.limit_kw)


async def run_telemetry_listener(
    cfg: Config,
    cache: MqttHubStatePort,
    *,
    username: str,
    password: str,
    l2_instructions: MqttL2InstructionPort | None = None,
) -> None:
    """Subscribe to `<root>/tel/#` (and, with `l2_instructions`, `<root>/scada/instruction/+`) for the
    process lifetime. Runs as a background task from `main.py`; a malformed message is logged and
    skipped, never fatal (K7)."""
    from opengrid.platform.mqtt import build_client

    instruction_prefix = topic(cfg, "scada/instruction/")
    async with build_client(cfg, username=username, password=password, process="guardian-tel") as client:
        await client.subscribe(topic(cfg, "tel/#"))
        if l2_instructions is not None:
            await client.subscribe(topic(cfg, "scada/instruction/+"))
        async for message in client.messages:
            payload = message.payload
            if not isinstance(payload, bytes | bytearray):
                continue
            msg_topic = str(message.topic)
            try:
                data = json.loads(payload)
                if l2_instructions is not None and msg_topic.startswith(instruction_prefix):
                    validate_payload("scada_utility_instruction", data)
                    l2_instructions.ingest(ScadaUtilityInstruction.model_validate(data))
                else:
                    validate_payload("telemetry", data)
                    cache.ingest(Telemetry.model_validate(data))
            except Exception:
                logger.exception("dropping malformed guardian input message", extra={"topic": msg_topic})


async def publish_command_batch(client: aiomqtt.Client, cfg: Config, batch: CommandBatch) -> None:
    """Publish the guardian-signed batch to `<root>/cmd/<bank_id>/batch` (QoS 1, not retained)."""
    validate_payload("command_batch", batch.model_dump(mode="json"))
    await client.publish(
        topic(cfg, f"cmd/{batch.bank_id}/batch"),
        payload=batch.model_dump_json().encode("utf-8"),
        qos=1,
        retain=False,
    )


async def publish_calibration_command(
    client: aiomqtt.Client, cfg: Config, command: CalibrationCommand
) -> None:
    """Publish a guardian-signed calibration command to `<root>/cmd/cal/<hub_id>` (QoS 1, not retained;
    S6.7). The published JSON is exactly the model the signature was computed over."""
    validate_payload("calibration_command", command.model_dump(mode="json"))
    await client.publish(
        topic(cfg, f"cmd/cal/{command.hub_id}"),
        payload=command.model_dump_json().encode("utf-8"),
        qos=1,
        retain=False,
    )


async def publish_lease(client: aiomqtt.Client, cfg: Config, lease: Lease) -> None:
    """Publish/renew the retained lease for `hub_id` on `<root>/lease/<hub_id>` (QoS 1, retained)."""
    validate_payload("lease", lease.model_dump(mode="json"))
    await client.publish(
        topic(cfg, f"lease/{lease.hub_id}"),
        payload=lease.model_dump_json().encode("utf-8"),
        qos=1,
        retain=True,
    )
