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
from ogsim.scada.grid_link import ScadaGridLinkBridge
from ogsim.scada.instructions import OverloadRule, lift_instruction, limit_instruction

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
        # Per-bank background load, scaled consistently with the existing per-bank fix
        # (background.py's own docstring/history-mean division): sized to the HOME bank roster only
        # (base + enabled zone blocks) -- a substation asset (below) is Base-owned generation/storage,
        # not a feeder segment with ~50 homes' worth of residential background load, so it must never
        # get a share of this model.
        self._home_bank_count = len(self.bank_ids)
        self.background = BackgroundLoadModel(
            self._home_bank_count, config.base_load_kw_default, config.history_tsv_path, self.rng
        )
        # Substation-sited battery-set assets (D11 SUBSTATION_BESS; OWNER DECISION D-29(b),
        # 2026-09-26): each enabled entry is its own bank, appended after every home bank, mirroring
        # `ogsim.fleet.state._substation_segment`'s `bank-<asset_id>` id scheme exactly so telemetry
        # ingested under that bank_id (published by `ogsim.fleet` as `bank-sub-LZ_AEN-00`, etc.)
        # actually lands in this engine's own buffer for it. Its kVA rating is derived from
        # `rated_mw`, not `bank_kva_rating_default` (a 600 kVA feeder-segment number would trip the
        # overload rule on a 20 MW asset from the first real reading).
        for asset in config.substation_assets:
            if not asset.enabled:
                continue
            bank_id = f"bank-{asset.asset_id}"
            self.bank_ids.append(bank_id)
            self.zones.append(asset.zone)
            self.kva_rating[bank_id] = kw_to_kva(asset.rated_mw * 1000.0)
        # Simulated mobile units (trucks, D-31): each is its own single-hub bank, measured at its rated
        # power (a charger/PCS rating, not a feeder segment) and, like a substation asset, with no
        # background residential load. Same `sim_bank_id` scheme as `ogsim.fleet.state._mobile_segment`.
        for unit in config.mobile_units:
            if not unit.simulate:
                continue
            self.bank_ids.append(unit.sim_bank_id)
            self.zones.append(unit.zone)
            self.kva_rating[unit.sim_bank_id] = kw_to_kva(unit.p_kw)
        self.buffers: dict[str, BankTelemetryBuffer] = {b: BankTelemetryBuffer() for b in self.bank_ids}
        self.anomalies = ScadaAnomalyManager(self.bank_ids, self.zones)
        self.overload_rule = OverloadRule(
            config.overload_consecutive_samples,
            clear_samples=config.overload_clear_samples,
            max_duration_s=config.overload_limit_max_duration_s,
        )

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
        # Sized to the HOME bank roster only (base + enabled zone blocks) -- see __init__'s
        # `_home_bank_count` comment for why a substation bank never gets a background-load share.
        noise = self.rng.normal(0.0, self.config.base_load_kw_default * 0.02, size=self._home_bank_count)
        background_kw = self.background.load_kw(now, noise)

        signals: list[tuple[str, dict[str, Any]]] = []
        instructions: list[tuple[str, dict[str, Any]]] = []
        for i, bank_id in enumerate(self.bank_ids):
            modifiers = self.anomalies.modifiers[bank_id]
            if modifiers.suppressed:
                continue
            bg_kw = float(background_kw[i]) if i < self._home_bank_count else 0.0
            real_kw = bank_load_kw(self.buffers[bank_id].net_battery_kw(), bg_kw)
            value_kw, quality = self.anomalies.apply_reading(
                bank_id, real_kw, "good", now, kva_rating=self.kva_rating[bank_id]
            )
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
            # Live bug fix, 2026-09-26 (R3, taken off Frank's #39 to avoid a duplicate): the guardian
            # now fails closed on unknown flow direction (G-30 vetoes discharge increases in
            # regulated territories without a signed real-power series). `value_kw` here is the same
            # post-anomaly reading the kVA signal above derives from (background load + net battery
            # power), so its sign is already the correct convention: + = the bank imports from the
            # feeder, - = export. Same ts/quality as the kVA signal; includes the substation bank
            # (this loop already covers every entry in self.bank_ids, home banks and substation
            # banks alike -- no separate branch needed).
            signals.append(
                (
                    f"scada/{bank_id}",
                    {
                        "bank_id": bank_id,
                        "signal": "REAL_POWER_KW",
                        "value": round(value_kw, 3),
                        "unit": "kW",
                        "quality": quality,
                        "ts": ts,
                    },
                )
            )
            pending = self.anomalies.take_pending_instruction(bank_id)
            if pending is not None:
                instructions.append((f"scada/instruction/{bank_id}", self._instruction_message(pending, now)))
                # R3.4 fix: a scenario-driven utility_instruction (BLOCK/ESTOP/LIMIT) now takes over
                # this bank's instruction slot -- forget this rule's own auto-LIMIT bookkeeping for it,
                # so a LATER auto-lift (`check_lift`) never fires and overwrites the scenario's own
                # instruction with an expired LIMIT (the orchestrator's fleet twin stores the LATEST
                # instruction per bank regardless of kind, so the auto-lift must only ever end an
                # auto-LIMIT it itself issued).
                self.overload_rule.cancel(bank_id)
            elif self.overload_rule.observe(bank_id, kva, self.kva_rating[bank_id], now):
                msg = limit_instruction(
                    str(uuid.uuid4()), bank_id, self.kva_rating[bank_id] * 0.9, utc_timestamp(now)
                )
                instructions.append((f"scada/instruction/{bank_id}", msg))
            elif self.overload_rule.check_lift(bank_id, kva, self.kva_rating[bank_id], now):
                # Bug fix, 2026-09-26 (R3): the auto-issued LIMIT above used to never expire, leaving
                # a bank_overload demo capped at 90% of rating forever. Lifted once the overload
                # clears for `clear_samples` readings, or after `max_duration_s`, whichever first.
                msg = lift_instruction(
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


async def run_scada(
    client: SimMqttClient,
    engine: ScadaEngine,
    clock: Clock,
    grid_link: ScadaGridLinkBridge | None = None,
) -> None:
    """Async shell: subscribes to fleet telemetry and scenario commands,
    ticks `engine` on `config.publish_interval_s`. Kept thin and not
    unit-tested (the engine above is). With `grid_link`, L2 instructions also
    (or only, when `also_mqtt` is false) go to OpenGrid over the grid-control link."""
    await client.subscribe("tel/#", qos=0)
    await client.subscribe("scenario/cmd", qos=1)
    while True:
        now = clock.now()
        signals, instructions = engine.tick(now)
        await client.publish_batch("scada_bank_signal", signals, qos=0)
        if grid_link is not None:
            grid_link.submit(instructions)
        if grid_link is None or grid_link.settings.also_mqtt:
            await client.publish_batch("scada_utility_instruction", instructions, qos=1)
        await clock.sleep(engine.config.publish_interval_s)
