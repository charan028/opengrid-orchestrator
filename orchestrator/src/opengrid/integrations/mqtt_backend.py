"""The default SCADA backend, `mqtt`: the existing `<root>/scada/<bank_id>` and
`<root>/scada/instruction/<bank_id>` topics (02b S6.2), today fed by `ogsim.scada`.

Those topics are already consumed directly by og-engine's ingest loop, og-guardian's L2 instruction
port and health, so this backend deliberately starts NOTHING: selecting `backend = "mqtt"` (the
default) leaves every process exactly as it was. It exists so every deployment has a `ScadaSource`
object to report on and so the protocol backends are drop-in alternatives behind one interface.
The protocol backends in production publish onto these same topics through `MqttBridgeSink`, which is
why the MQTT topic contract stays the single ingest path.
"""

from __future__ import annotations

import asyncio

from opengrid.integrations.interfaces import ScadaSink

__all__ = ["MqttScadaSource"]


class MqttScadaSource:
    """Passthrough `ScadaSource`: the engine/guardian MQTT subscriptions are the implementation."""

    @property
    def backend(self) -> str:
        return "mqtt"

    async def run(self, sink: ScadaSink) -> None:
        """Nothing to drive: park until cancelled, so callers can treat every backend uniformly."""
        await asyncio.Event().wait()

    async def poll_once(self, sink: ScadaSink) -> int:
        return 0

    async def close(self) -> None:
        return None
