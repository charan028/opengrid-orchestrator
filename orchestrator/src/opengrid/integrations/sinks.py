"""`ScadaSink` implementations: where a protocol adapter delivers what it reads.

- `MqttBridgeSink` (recommended in production): republishes every reading/instruction on the EXISTING
  topics `<root>/scada/<bank_id>` and `<root>/scada/instruction/<bank_id>`, validated against the same
  `interfaces/mqtt` schemas. The engine's ingest loop, the guardian's independent L2 instruction port
  (G-15) and health keep their own subscriptions unchanged, so guardian independence (K5) is kept.
- `FleetScadaSink`: in-process delivery straight into `opengrid.fleet` (the same two ingest functions the
  engine's MQTT loop calls). Useful when the adapter runs inside og-engine; the guardian then does NOT
  see instructions, so it is only acceptable together with a bridge for instructions.
- `CollectingSink`: keeps everything in memory (tests, "read now" diagnostics).
"""

from __future__ import annotations

import json
from typing import Protocol

from opengrid.core.models.mqtt import ScadaBankSignal, ScadaUtilityInstruction
from opengrid.platform.mqtt import validate_payload

__all__ = ["CollectingSink", "FleetScadaSink", "MqttBridgeSink", "MqttPublisher"]


class MqttPublisher(Protocol):
    """The subset of `aiomqtt.Client` the bridge needs."""

    async def publish(self, topic: str, payload: str, qos: int = 0) -> object: ...


class MqttBridgeSink:
    """Republish adapter output on the existing SCADA topics (QoS 0 signals, QoS 1 instructions,
    matching `ogsim.scada` and 02b S6.2)."""

    def __init__(self, publisher: MqttPublisher, topic_root: str) -> None:
        self._publisher = publisher
        self._root = topic_root.rstrip("/")

    async def on_bank_signal(self, signal: ScadaBankSignal) -> None:
        payload = signal.model_dump(mode="json")
        validate_payload("scada_bank_signal", payload)
        await self._publisher.publish(f"{self._root}/scada/{signal.bank_id}", json.dumps(payload), qos=0)

    async def on_utility_instruction(self, instruction: ScadaUtilityInstruction) -> None:
        payload = instruction.model_dump(mode="json")
        validate_payload("scada_utility_instruction", payload)
        await self._publisher.publish(
            f"{self._root}/scada/instruction/{instruction.bank_id}", json.dumps(payload), qos=1
        )


class FleetScadaSink:
    """Deliver in-process into the fleet twin (`opengrid.fleet.ingest_scada_signal` /
    `ingest_utility_instruction`), exactly as the engine's MQTT ingest loop does."""

    async def on_bank_signal(self, signal: ScadaBankSignal) -> None:
        from opengrid import fleet

        await fleet.ingest_scada_signal(signal.model_dump(mode="json"))

    async def on_utility_instruction(self, instruction: ScadaUtilityInstruction) -> None:
        from opengrid import fleet

        await fleet.ingest_utility_instruction(instruction.model_dump(mode="json"))


class CollectingSink:
    """In-memory sink: every delivered item, in arrival order."""

    def __init__(self) -> None:
        self.signals: list[ScadaBankSignal] = []
        self.instructions: list[ScadaUtilityInstruction] = []

    async def on_bank_signal(self, signal: ScadaBankSignal) -> None:
        self.signals.append(signal)

    async def on_utility_instruction(self, instruction: ScadaUtilityInstruction) -> None:
        self.instructions.append(instruction)

    def latest(self, bank_id: str, signal: str) -> ScadaBankSignal | None:
        for item in reversed(self.signals):
            if item.bank_id == bank_id and item.signal == signal:
                return item
        return None
