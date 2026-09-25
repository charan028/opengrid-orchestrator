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
from typing import Any

from ogsim.common.clock import RealClock
from ogsim.common.config import load_fleet_config
from ogsim.common.mqtt_client import AiomqttTransportAdapter, SimMqttClient, mqtt_settings
from ogsim.fleet.runtime import FleetEngine, load_guardian_public_key, run_fleet

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
    engine = FleetEngine(config)
    clock = RealClock()

    async with aiomqtt.Client(
        **mqtt_settings(config.mqtt.host, config.mqtt.port, config.mqtt.password)
    ) as raw:
        client = SimMqttClient(AiomqttTransportAdapter(raw), config.mqtt.topic_root)

        async def consume() -> None:
            async for msg in client.messages():
                _dispatch_message(engine, str(msg.topic), msg.payload, public_key)

        await asyncio.gather(consume(), run_fleet(client, engine, clock, public_key))


def _dispatch_message(engine: FleetEngine, topic: str, payload: Any, public_key: Any) -> None:
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        logger.warning("dropping unparseable message on %s", topic)
        return
    parts = topic.split("/")
    if "/cmd/" in topic and topic.endswith("/batch"):
        engine.handle_command_batch(data, public_key, time.time())
    elif "/stop/" in topic:
        scope = parts[-2]
        scope_id = None if scope == "fleet" else parts[-1]
        action = data.get("action", "RELEASE") if data else "RELEASE"
        engine.handle_stop_event(action, scope, scope_id)
    elif "/lease/" in topic and data:
        engine.handle_lease_message(parts[-1], data["expires_at"])
    elif topic.endswith("/scenario/cmd"):
        engine.handle_scenario_cmd(data)


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
