"""ogsim.scada.runtime -- ScadaEngine: per-tick bank aggregation + anomalies,
plus the thin async MQTT shell.

`ScadaEngine` is pure (no I/O) and unit-testable with a `FakeClock`;
`run_scada` is the async glue that feeds it from `ogsim.common.mqtt_client`.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import numpy as np

from ogsim.common.clock import Clock
from ogsim.common.config import ScadaConfig, bank_topology
from ogsim.common.mqtt_client import SimMqttClient
from ogsim.common.scenario import parse_scenario_cmd, utc_timestamp
from ogsim.scada.aggregation import BankTelemetryBuffer, bank_load_kw, kw_to_kva
from ogsim.scada.anomalies import SCADA_ANOMALY_TYPES, ScadaAnomalyManager
from ogsim.scada.background import BackgroundLoadModel
from ogsim.scada.instructions import OverloadRule, limit_instruction

logger = logging.getLogger(__name__)


class ScadaEngine:
    """Pure per-tick SCADA logic: aggregate fleet telemetry per bank, apply
    anomalies, evaluate the overload rule, build outbound messages."""

    def __init__(self, config: ScadaConfig, seed: int = 0) -> None:
        self.config = config
        self.rng = np.random.default_rng(seed)
        # Blocker fix (zone blocks, build phase 2026-09-26): covers every bank in the fleet
        # topology -- the base bank_count banks PLUS every ENABLED entry in config.zone_blocks
        # (LZ_AEN/LZ_CPS, bank-040+) -- not just the base fleet. `bank_topology` uses the exact
        # same id-numbering/offset scheme `ogsim.fleet.state.build_fleet_state` uses for hubs,
        # so a block enabled in both fleet.yaml and scada.yaml gets the identical bank roster
        # on both sides. Blocks stay disabled by default, so `self.bank_ids` is unchanged from
        # before this fix (still exactly `bank-000..039`) until a block is turned on.
        self.bank_ids, self.zones = bank_topology(config.bank_count, config.zones, config.zone_blocks)
        self.kva_rating = {b: config.bank_kva_rating_default for b in self.bank_ids}
        self.buffers: dict[str, BankTelemetryBuffer] = {b: BankTelemetryBuffer() for b in self.bank_ids}
        # Per-bank background load, scaled consistently with the existing per-bank fix
        # (background.py's own docstring/history-mean division): sized to the FULL bank
        # roster above (base + enabled blocks), not just config.bank_count, so a block's banks
        # get their own ~1/len(bank_ids) share instead of either going unmodeled or replaying
        # the whole substation's history mean onto each of them.
        self.background = BackgroundLoadModel(
            len(self.bank_ids), config.base_load_kw_default, config.history_tsv_path, self.rng
        )
        self.anomalies = ScadaAnomalyManager(self.bank_ids, self.zones)
        self.overload_rule = OverloadRule(config.overload_consecutive_samples)

    def ingest_telemetry(self, hub_id: str, bank_id: str, p_kw: float) -> None:
        buffer = self.buffers.get(bank_id)
        if buffer is not None:
            buffer.update(hub_id, p_kw)

    def handle_scenario_cmd(self, raw: dict[str, Any]) -> bool:
        cmd = parse_scenario_cmd(raw)
        if cmd.catalogue_type not in SCADA_ANOMALY_TYPES:
            return False
        self.anomalies.start(
            cmd.id,
            cmd.catalogue_type,
            cmd.target_kind,
            cmd.target_ref,
            cmd.params,
            cmd.start_epoch,
            cmd.duration_s,
        )
        return True

    def tick(self, now: float) -> tuple[list[tuple[str, dict[str, Any]]], list[tuple[str, dict[str, Any]]]]:
        """Returns `(bank_signal_messages, instruction_messages)` as
        `(topic_suffix, message)` pairs for this tick."""
        self.anomalies.tick(now)
        # Sized to the full bank roster (base + enabled blocks, see __init__), matching
        # `self.background`'s own shape -- `config.bank_count` alone under-sized this once a
        # block was enabled, which would raise a numpy broadcast error in `load_kw` below.
        noise = self.rng.normal(0.0, self.config.base_load_kw_default * 0.02, size=len(self.bank_ids))
        background_kw = self.background.load_kw(now, noise)

        signals: list[tuple[str, dict[str, Any]]] = []
        instructions: list[tuple[str, dict[str, Any]]] = []
        for i, bank_id in enumerate(self.bank_ids):
            modifiers = self.anomalies.modifiers[bank_id]
            if modifiers.suppressed:
                continue
            real_kw = bank_load_kw(self.buffers[bank_id].net_battery_kw(), float(background_kw[i]))
            value_kw, quality = self.anomalies.apply_reading(bank_id, real_kw, "good", now)
            kva = kw_to_kva(value_kw)
            ts = utc_timestamp(now + modifiers.time_skew_s)
            signals.append(
                (
                    f"scada/{bank_id}",
                    {
                        "bank_id": bank_id,
                        "signal": "APPARENT_POWER_KVA",
                        "value": round(kva, 3),
                        "unit": "kVA",
                        "quality": quality,
                        "ts": ts,
                    },
                )
            )
            pending = self.anomalies.take_pending_instruction(bank_id)
            if pending is not None:
                instructions.append((f"scada/instruction/{bank_id}", self._instruction_message(pending, now)))
            elif self.overload_rule.observe(bank_id, kva, self.kva_rating[bank_id]):
                msg = limit_instruction(
                    str(uuid.uuid4()), bank_id, self.kva_rating[bank_id] * 0.9, utc_timestamp(now)
                )
                instructions.append((f"scada/instruction/{bank_id}", msg))
        return signals, instructions

    def _instruction_message(self, pending: dict[str, Any], now: float) -> dict[str, Any]:
        """Blocker fix: `pending["lift"]` (set by `ScadaAnomalyManager._revert`'s
        `utility_instruction` branch) sets `expires_at` to THIS message's own `issued_at` --
        already in the past the instant it arrives -- instead of `None` ("never expires").
        `opengrid.fleet._active_utility_limit_kw` already treats `expires_at <= now` as
        inactive (the SAME mechanism the rule-based auto-instruction relies on to eventually
        age out), so this needs no orchestrator-side change: it is the model's own existing
        way an instruction ends, applied here explicitly instead of never being used."""
        ts = utc_timestamp(now)
        return {
            "instruction_id": str(uuid.uuid4()),
            "bank_id": pending["bank_id"],
            "kind": pending["kind"],
            "limit_kw": pending.get("limit_kw"),
            "issued_at": ts,
            "expires_at": ts if pending.get("lift") else None,
            "issued_by": "SCENARIO_ANOMALY",
        }


async def run_scada(client: SimMqttClient, engine: ScadaEngine, clock: Clock) -> None:
    """Async shell: subscribes to fleet telemetry and scenario commands,
    ticks `engine` on `config.publish_interval_s`. Kept thin and not
    unit-tested (the engine above is)."""
    await client.subscribe("tel/#", qos=0)
    await client.subscribe("scenario/cmd", qos=1)
    while True:
        now = clock.now()
        signals, instructions = engine.tick(now)
        await client.publish_batch("scada_bank_signal", signals, qos=0)
        await client.publish_batch("scada_utility_instruction", instructions, qos=1)
        await clock.sleep(engine.config.publish_interval_s)
