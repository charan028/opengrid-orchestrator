"""ogsim.fleet CLI entry point: `python -m ogsim.fleet`.

Normal mode connects to MQTT and runs forever. `--no-mqtt --duration-s N`
runs a standalone benchmark: ticks the vectorized fleet state for N
seconds of simulated time with no network I/O, and reports elapsed
wall-clock time and CPU time per tick (02b §5 CPU measurement ask).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema

from ogsim.common.clock import RealClock
from ogsim.common.config import load_fleet_config
from ogsim.common.mqtt_client import AiomqttTransportAdapter, SimMqttClient, mqtt_settings
from ogsim.fleet.runtime import (
    FleetEngine,
    load_guardian_public_key,
    load_safestop_public_key,
    run_fleet,
)

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO, format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}'
    )


def run_benchmark(hub_count: int, duration_s: float, tick_interval_s: float) -> dict[str, Any]:
    """Ticks a `FleetEngine` for `duration_s` of simulated time, no MQTT."""
    config = load_fleet_config()
    config = _with_hub_count(config, hub_count)
    engine = FleetEngine(config, seed=1)
    n_ticks = max(1, int(duration_s / tick_interval_s))

    # time.process_time() (CPU time for this process, user+system) is used
    # rather than resource.getrusage because `resource` is POSIX-only and
    # this benchmark must also run on the Windows dev venv (BUILD.md's
    # "whichever is more practical" CPU-measurement instruction).
    cpu_start = time.process_time()
    wall_start = time.perf_counter()
    now = 0.0
    for _ in range(n_ticks):
        engine.tick(now)
        _ = engine.telemetry_messages(now)
        now += tick_interval_s
    wall_elapsed = time.perf_counter() - wall_start
    cpu_elapsed = time.process_time() - cpu_start

    return {
        "hub_count": hub_count,
        "ticks": n_ticks,
        "wall_s": wall_elapsed,
        "cpu_s": cpu_elapsed,
        "wall_s_per_tick": wall_elapsed / n_ticks,
        "cpu_s_per_tick": cpu_elapsed / n_ticks,
    }


def _with_hub_count(config: Any, hub_count: int) -> Any:
    from dataclasses import replace

    return replace(config, hub_count=hub_count)


async def _run_forever() -> None:
    import aiomqtt

    config = load_fleet_config()
    public_key = load_guardian_public_key(config)
    safestop_public_key = load_safestop_public_key(config)
    engine = FleetEngine(config)
    clock = RealClock()

    async with aiomqtt.Client(
        **mqtt_settings(config.mqtt.host, config.mqtt.port, config.mqtt.password, config.mqtt.username)
    ) as raw:
        client = SimMqttClient(AiomqttTransportAdapter(raw), config.mqtt.topic_root)

        async def consume() -> None:
            async for msg in client.messages():
                await _dispatch_message(
                    engine, client, str(msg.topic), msg.payload, public_key, safestop_public_key
                )

        await asyncio.gather(consume(), run_fleet(client, engine, clock, public_key))


def handle_stop_message(
    engine: FleetEngine, topic: str, data: Any, safestop_public_key: Any, guardian_public_key: Any
) -> bool:
    """K8 / crypto.md S2.3: stop state changes ONLY through a signature-verified StopEvent -- ENGAGE
    signed by the safestop key, RELEASE signed by the guardian key (`FleetEngine.handle_stop_event`).
    The event's own signed `scope`/`scope_id` govern, never the topic. Anything that is not a non-empty
    JSON object ({}, null, [], 0, false, ...) is broker housekeeping (clearing a retained message) and
    never changes stop state -- an unsigned "empty" payload must not be able to release a stop.
    Returns True only when a verified event was applied."""
    if not isinstance(data, dict) or not data:
        logger.info("ignoring non-event payload on %s: it never changes stop state", topic)
        return False
    return engine.handle_stop_event(data, safestop_public_key, guardian_public_key)


async def _dispatch_message(
    engine: FleetEngine,
    client: SimMqttClient,
    topic: str,
    payload: Any,
    public_key: Any,
    safestop_public_key: Any,
) -> None:
    if isinstance(payload, bytes | bytearray) and not payload:
        # Zero-length: the broker clearing a retained message. Never a command or a stop-state change.
        return
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        logger.warning("dropping unparseable message on %s", topic)
        return
    parts = topic.split("/")
    if "/cmd/" in topic and topic.endswith("/batch"):
        await _handle_command_batch(engine, client, data, public_key)
    elif "/scada/wave/" in topic and topic.endswith("/request"):
        # WP-H (06-service-profiles-and-power-quality.md S6.4b): on-demand waveform
        # capture trigger. `data` is already a `waveform_capture_request.schema.json`
        # object (validated by the ORCHESTRATOR before it publishes -- this sim, like
        # `_handle_command_batch`, validates its OWN outbound publish via
        # `publish_validated`, not every inbound message; a malformed/expired request
        # simply yields no raw-capture response, per §6.4b).
        result = engine.handle_wave_capture_request(data, time.time())
        if result is not None:
            suffix, message = result
            await client.publish_validated("pq_waveform_raw", suffix, message, qos=1)
    elif "/cmd/cal/" in topic:
        # S6.7 (WP-I): guardian-signed remote-calibration command. `data` is a
        # calibration_command.schema.json object; verification/application is entirely
        # `ogsim.fleet.calibration.apply_calibration` via `engine.handle_calibration_
        # command` (unmodified) -- this branch only routes the message in and the ack
        # back out.
        await _handle_calibration_command(engine, client, data, public_key)
    elif "/stop/" in topic:
        handle_stop_message(engine, topic, data, safestop_public_key, public_key)
    elif "/lease/" in topic and data:
        engine.handle_lease_message(parts[-1], data["expires_at"])
    elif topic.endswith("/scenario/cmd"):
        engine.handle_scenario_cmd(data)


async def _handle_command_batch(
    engine: FleetEngine, client: SimMqttClient, batch: dict[str, Any], public_key: Any
) -> None:
    """Evaluates `batch` and publishes one ack per verdict to
    `<root>/ack/<hub_id>` (ack.schema.json), QoS 1 -- every command verdict
    gets an ack, accepted or rejected."""
    verdicts = engine.handle_command_batch(batch, public_key, time.time())
    batch_id = str(batch.get("batch_id", ""))
    now = time.time()
    for verdict in verdicts:
        ack = engine.build_ack(verdict, batch_id, now)
        await client.publish_validated("ack", f"ack/{verdict.hub_id}", ack, qos=1)


#: calibration_ack.schema.json's own field set (additionalProperties: false). `ogsim.fleet.
#: calibration.build_calibration_ack` also returns an internal `outcome`/`reject_reason` pair this
#: sim's own scenario assertions use -- narrowed here to the wire schema before publish, never a change
#: to `calibration.py`'s own outcome-computation logic.
_CALIBRATION_ACK_WIRE_FIELDS = frozenset(
    {
        "calibration_id",
        "hub_id",
        "applied",
        "applied_at",
        "resulting_offsets",
        "status",
        "epoch",
        "seq",
        "reject_reason",
    }
)


@lru_cache(maxsize=1)
def _calibration_ack_validator() -> jsonschema.protocols.Validator:
    """Loads `interfaces/mqtt/calibration_ack.schema.json` directly (mirrors `ogsim.common.schemas`'s
    own technique) rather than adding an entry to that module's `_SCHEMA_NAMES` table, which is outside
    this file's edit scope for this change -- see the build report for the one-line addition that would
    let this go through `client.publish_validated("calibration_ack", ...)` instead."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "interfaces" / "mqtt" / "calibration_ack.schema.json"
        if candidate.is_file():
            with candidate.open(encoding="utf-8") as fh:
                schema: dict[str, Any] = json.load(fh)
            validator_cls = jsonschema.validators.validator_for(schema)
            validator_cls.check_schema(schema)
            return validator_cls(schema)
    raise FileNotFoundError("calibration_ack.schema.json not found from ogsim.fleet.__main__")


async def _handle_calibration_command(
    engine: FleetEngine, client: SimMqttClient, command: dict[str, Any], public_key: Any
) -> None:
    """S6.7: verifies+applies `command` (`FleetEngine.handle_calibration_command`, entirely delegating
    to the unmodified `ogsim.fleet.calibration` logic) and publishes the resulting `CalibrationAck` on
    `<root>/ack/cal/<hub_id>`, QoS 1. Uses the transport directly (not `publish_validated`, see
    `_calibration_ack_validator`'s docstring) after this file's own schema check."""
    hub_id = str(command.get("hub_id", ""))
    ack = engine.handle_calibration_command(command, public_key, time.time())
    wire_ack = {k: v for k, v in ack.items() if k in _CALIBRATION_ACK_WIRE_FIELDS}
    errors = sorted(_calibration_ack_validator().iter_errors(wire_ack), key=lambda e: e.path)
    if errors:
        logger.warning("calibration ack failed schema validation hub_id=%s: %s", hub_id, errors[0].message)
        return
    payload = json.dumps(wire_ack).encode("utf-8")
    await client._transport.publish(client.topic(f"ack/cal/{hub_id}"), payload=payload, qos=1)  # noqa: SLF001


def main() -> None:
    _configure_logging()
    parser = argparse.ArgumentParser(prog="ogsim.fleet")
    parser.add_argument("--no-mqtt", action="store_true", help="run the standalone benchmark, no network I/O")
    parser.add_argument("--hub-count", type=int, default=2000)
    parser.add_argument("--duration-s", type=float, default=10.0)
    parser.add_argument("--tick-interval-s", type=float, default=2.0)
    args = parser.parse_args()

    if args.no_mqtt:
        result = run_benchmark(args.hub_count, args.duration_s, args.tick_interval_s)
        logger.info("benchmark result: %s", json.dumps(result))
        return
    asyncio.run(_run_forever())


if __name__ == "__main__":
    main()
