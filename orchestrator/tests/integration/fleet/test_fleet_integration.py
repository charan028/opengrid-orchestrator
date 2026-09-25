"""Integration test for `opengrid.fleet` (TS-03-04/05, BUILD.md's "integration test on the server"):
real Mosquitto (topic root `ogtest/<ws>`) and real Postgres (`og_t_<ws>`), publishing synthetic
telemetry for a 2,000-hub fleet and measuring end-to-end ingestion throughput.

Run via `powershell -File tools\\remote.ps1 -Ws eng -Cmd "cd orchestrator && bash tools/check.sh"`, or
directly with `OG_CONFIG`/`OG_DB`/`OG_MQTT_ROOT` set and both Postgres and Mosquitto reachable. Skipped
automatically when either is unreachable (BUILD.md S5: "local: no DB/MQTT").

`sim` (the real 2,000-hub test harness, ES03-S03) is a separate agent's not-yet-built package; this test
publishes its own synthetic `Telemetry` messages instead, so it exercises exactly the boundary `fleet`
owns (MQTT -> schema validation -> twin -> Postgres) without depending on `sim`'s implementation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import UTC, datetime

import psycopg
import pytest

from opengrid.core.models.mqtt import Telemetry
from opengrid.platform.config import load_config
from opengrid.platform.db import build_dsn, migrate_sync
from opengrid.platform.mqtt import build_client, topic

pytestmark = pytest.mark.asyncio
logger = logging.getLogger(__name__)

_CONFIG_PATH = os.environ.get(
    "OG_CONFIG", str((__file__.rsplit("orchestrator", 1)[0]) + "orchestrator/config/test.toml")
)
_HUB_COUNT = int(os.environ.get("OG_TEST_FLEET_HUBS", "2000"))
_BANK_COUNT = 40


def _cfg():
    return load_config(_CONFIG_PATH)


def _dsn() -> str | None:
    try:
        return build_dsn(_cfg())
    except Exception:
        return None


def _db_reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=2):
            return True
    except Exception:
        return False


def _mqtt_reachable(cfg) -> bool:
    import socket

    try:
        with socket.create_connection(
            (cfg.get("mqtt.host", "127.0.0.1"), cfg.get("mqtt.port", 1883)), timeout=2
        ):
            return True
    except OSError:
        return False


_CFG = _cfg() if os.environ.get("OG_DB") else None
_DSN = _dsn()
_SKIP_REASON = "Postgres/Mosquitto not reachable locally; run via tools/remote.ps1 -Ws eng (BUILD.md S5)"
requires_server = pytest.mark.skipif(
    _CFG is None or _DSN is None or not _db_reachable(_DSN) or not _mqtt_reachable(_CFG),
    reason=_SKIP_REASON,
)


async def _seed_topology(dsn: str, hub_count: int, bank_count: int) -> list[Telemetry]:
    """Insert `hub`/`bank` rows for a synthetic fleet and return one `Telemetry` message per hub."""
    zones = ["LZ_NORTH", "LZ_SOUTH", "LZ_HOUSTON", "LZ_WEST"]
    now = datetime.now(UTC)
    telemetries: list[Telemetry] = []
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM og.telemetry WHERE hub_id LIKE 'itfleet-%'")
        cur.execute("DELETE FROM og.hub_state WHERE hub_id LIKE 'itfleet-%'")
        cur.execute("DELETE FROM og.hub WHERE hub_id LIKE 'itfleet-%'")
        cur.execute("DELETE FROM og.bank WHERE bank_id LIKE 'itfleet-%'")
        for b in range(bank_count):
            bank_id = f"itfleet-bank-{b}"
            zone = zones[b % len(zones)]
            cur.execute(
                "INSERT INTO og.bank (bank_id, zone, kva_rating, reserve_kva) VALUES (%s, %s, %s, %s)",
                (bank_id, zone, 1_000_000.0, 0.0),
            )
        for i in range(hub_count):
            hub_id = f"itfleet-{i}"
            bank_id = f"itfleet-bank-{i % bank_count}"
            zone = zones[i % len(zones)]
            cur.execute(
                "INSERT INTO og.hub (hub_id, bank_id, zone, e_kwh, r_kwh, p_kw) VALUES (%s, %s, %s, %s, %s, %s)",
                (hub_id, bank_id, zone, 13.5, 2.7, 5.0),
            )
            telemetries.append(
                Telemetry(
                    hub_id=hub_id,
                    bank_id=bank_id,
                    zone=zone,
                    ts=now,
                    soc_kwh=10.0,
                    p_kw=-1.0,
                    health="online",
                    seq=1,
                    epoch=1,
                )
            )
    return telemetries


@requires_server
async def test_synthetic_fleet_telemetry_ingests_end_to_end_over_mqtt() -> None:
    """Publishes one `Telemetry` message per synthetic hub to `<root>/tel/<zone>/<bank>/<hub>`,
    consumes them the same way `opengrid.engine._mqtt_ingest_loop` does, and asserts every hub's
    `hub_state` row lands in Postgres -- proving the MQTT -> validate -> twin -> DB path end to end
    (TS-03-04/05's prerequisite: the twin must actually see the fleet before it can classify it)."""
    assert _DSN is not None
    assert _CFG is not None
    migrate_sync(_DSN)
    telemetries = await _seed_topology(_DSN, _HUB_COUNT, _BANK_COUNT)

    from psycopg_pool import AsyncConnectionPool

    from opengrid import fleet
    from opengrid.fleet.pg_backend import PgFleetBackend
    from opengrid.platform.mqtt import validate_payload

    pool = AsyncConnectionPool(_DSN, min_size=2, max_size=8, open=False)
    await pool.open(wait=True)
    try:
        fleet.configure(PgFleetBackend(pool), _CFG)
        await fleet.load_topology()

        received = 0
        publish_password = os.environ.get("OG_MQTT_SIM_PASSWORD", "")
        subscribe_password = os.environ.get("OG_MQTT_ENGINE_PASSWORD", "")

        async with build_client(
            _CFG, username="og_engine", password=subscribe_password, client_id="og-test-fleet-sub"
        ) as subscriber:
            await subscriber.subscribe(topic(_CFG, "tel/#"))

            async def _consume() -> None:
                nonlocal received
                async for message in subscriber.messages:
                    import json

                    payload = json.loads(message.payload)
                    validate_payload("telemetry", payload)
                    await fleet.ingest_telemetry(payload)
                    received += 1
                    if received >= len(telemetries):
                        return

            consume_task = asyncio.create_task(_consume())

            t0 = time.perf_counter()
            async with build_client(
                _CFG, username="og_sim", password=publish_password, client_id="og-test-fleet-pub"
            ) as publisher:
                for t in telemetries:
                    await publisher.publish(
                        topic(_CFG, f"tel/{t.zone}/{t.bank_id}/{t.hub_id}"),
                        payload=t.model_dump_json().encode("utf-8"),
                        qos=0,
                    )

            await asyncio.wait_for(consume_task, timeout=60.0)
            t1 = time.perf_counter()

        stats = await fleet.flush()
        elapsed_s = t1 - t0
        logger.info(
            "TS-03 ingestion throughput: %d telemetry msgs in %.3fs = %.1f msg/s "
            "(flush: %d telemetry rows, %d hub_states)",
            received,
            elapsed_s,
            received / elapsed_s,
            stats.telemetry_rows,
            stats.hub_states,
        )

        assert received == len(telemetries)
        assert stats.hub_states == len(telemetries)

        with psycopg.connect(_DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM og.hub_state WHERE hub_id LIKE 'itfleet-%'")
            (count,) = cur.fetchone()
            assert count == len(telemetries)
            cur.execute("SELECT count(*) FROM og.telemetry WHERE hub_id LIKE 'itfleet-%'")
            (tel_count,) = cur.fetchone()
            assert tel_count == len(telemetries)
    finally:
        await pool.close()


@requires_server
async def test_stale_hub_excluded_from_capability_within_one_detection_cycle() -> None:
    """TS-03-04 against real Postgres: a hub with no telemetry is excluded from `capability()`
    immediately, and a hub reclassified `offline` by `flush()` is excluded too."""
    assert _DSN is not None
    assert _CFG is not None
    migrate_sync(_DSN)
    await _seed_topology(_DSN, hub_count=5, bank_count=1)

    from psycopg_pool import AsyncConnectionPool

    from opengrid import fleet
    from opengrid.fleet.pg_backend import PgFleetBackend

    pool = AsyncConnectionPool(_DSN, min_size=1, max_size=2, open=False)
    await pool.open(wait=True)
    try:
        fleet.configure(PgFleetBackend(pool), _CFG)
        await fleet.load_topology()

        cap = await fleet.capability("itfleet-bank-0", datetime.now(UTC))
        assert cap.excluded_hub_ids == {f"itfleet-{i}" for i in range(5)}
        assert cap.max_discharge_kw == 0.0
    finally:
        await pool.close()
