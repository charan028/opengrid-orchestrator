"""`og-guardian` process entry point (BUILD.md S4 process table; `python -m opengrid.guardian.main`).

Wires the real Postgres/MQTT-backed ports (`opengrid.guardian.repo`/`mqtt_io`), configures the module-
level `opengrid.guardian.evaluate_and_sign` singleton, and runs the guardian's own cycle: pick up
proposed batches that have no verdict yet, evaluate and (if PASS) publish the signed command batch and
renew the bank's hubs' leases. Heartbeats every cycle (02b S6.4).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os

import aiomqtt
from prometheus_client import start_http_server
from psycopg_pool import AsyncConnectionPool

import opengrid.guardian as guardian_module
from opengrid.core.models.engine import CommandBatchRow, Verdict
from opengrid.core.models.mqtt import CommandBatch, CommandItem, Lease
from opengrid.guardian.config import load_guardian_config
from opengrid.guardian.keys import resolve_signing_seed
from opengrid.guardian.mqtt_io import (
    MqttHubStatePort,
    publish_command_batch,
    publish_lease,
    run_telemetry_listener,
)
from opengrid.guardian.ports import ProposedBatch
from opengrid.guardian.repo import build_pg_ports, load_hub_params
from opengrid.guardian.service import GuardianService
from opengrid.platform.config import Config, load_config, resolve_secret
from opengrid.platform.db import build_dsn
from opengrid.platform.heartbeat import write_heartbeat
from opengrid.platform.log import configure_logging
from opengrid.platform.mqtt import build_client
from opengrid.platform.process import run_forever
from opengrid.trace import TraceStore
from opengrid.trace.pg_backend import PgTraceBackend

logger = logging.getLogger("guardian")

_PENDING_BATCHES_SQL = """
SELECT cb.command_batch_id, cb.cycle_id, cb.ledger_version, cb.submission_id, cb.command_count, cb.merkle_root,
       cb.trace_pre_image_id
FROM og.command_batch cb
LEFT JOIN og.verdict v ON v.command_batch_id = cb.command_batch_id
WHERE v.verdict_id IS NULL
ORDER BY cb.created_at
LIMIT 50
"""

_INSERT_VERDICT_SQL = """
INSERT INTO og.verdict (verdict_id, command_batch_id, outcome, vetoed_rule_ids, latency_ms, inputs_hash,
                         signature, signed_at)
VALUES (%(verdict_id)s, %(command_batch_id)s, %(outcome)s, %(vetoed_rule_ids)s, %(latency_ms)s,
        %(inputs_hash)s, %(signature)s, %(signed_at)s)
"""


async def _publish_signed_batch(
    *, mqtt_client: aiomqtt.Client, cfg: Config, key_id: str, verdict: Verdict, proposal: ProposedBatch
) -> None:
    """On a PASS verdict, publish the signed command batch and renew the bank's hub leases."""
    batch = CommandBatch(
        batch_id=proposal.command_batch_id,
        bank_id=proposal.bank_id,
        epoch=proposal.epoch,
        seq=proposal.seq,
        issued_at=proposal.issued_at,
        expires_at=proposal.expires_at,
        items=[
            CommandItem(hub_id=i.hub_id, p_kw_setpoint=i.p_kw_setpoint, reason_code=i.reason_code)
            for i in proposal.items
        ],
        key_id=key_id,
        signature=verdict.signature or "",
    )
    await publish_command_batch(mqtt_client, cfg, batch)
    for item in proposal.items:
        lease = Lease(
            hub_id=item.hub_id,
            epoch=proposal.epoch,
            expires_at=proposal.expires_at,
            issued_at=proposal.issued_at,
        )
        await publish_lease(mqtt_client, cfg, lease)


async def _fetch_pending_batches(pool: AsyncConnectionPool) -> list[CommandBatchRow]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_PENDING_BATCHES_SQL)
        rows = await cur.fetchall()
    return [
        CommandBatchRow(
            command_batch_id=row[0],
            cycle_id=row[1],
            ledger_version=row[2],
            submission_id=row[3],
            command_count=row[4],
            merkle_root=row[5],
            trace_pre_image_id=row[6],
        )
        for row in rows
    ]


async def _insert_verdict(pool: AsyncConnectionPool, verdict: Verdict) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            _INSERT_VERDICT_SQL,
            {
                "verdict_id": verdict.verdict_id,
                "command_batch_id": verdict.command_batch_id,
                "outcome": verdict.outcome,
                "vetoed_rule_ids": verdict.vetoed_rule_ids,
                "signature": verdict.signature,
                "signed_at": verdict.signed_at,
                "latency_ms": verdict.latency_ms,
                "inputs_hash": verdict.inputs_hash,
            },
        )
        await conn.commit()


async def main() -> None:
    configure_logging("guardian")
    cfg = load_config(os.environ.get("OG_CONFIG"))
    guardian_cfg = load_guardian_config(cfg)

    signing_seed = resolve_signing_seed(
        env_var_name=guardian_cfg.signing_seed_env, key_path=guardian_cfg.key_path
    )

    pool = AsyncConnectionPool(build_dsn(cfg), min_size=1, max_size=4, open=False)
    await pool.open(wait=True)

    trace_store = TraceStore(PgTraceBackend(pool))
    ports, leases = build_pg_ports(pool, trace_store)

    telemetry_cache = MqttHubStatePort(await load_hub_params(pool))
    ports = dataclasses.replace(ports, hubs=telemetry_cache)

    service = GuardianService(ports=ports, config=guardian_cfg, signing_seed=signing_seed)
    guardian_module.configure(service)

    mqtt_password = resolve_secret("OG_MQTT_GUARDIAN_PASSWORD")
    telemetry_task = asyncio.create_task(
        run_telemetry_listener(cfg, telemetry_cache, username="og_guardian", password=mqtt_password)
    )
    start_http_server(
        int(cfg.get("metrics.guardian_port", 9103)), addr=str(cfg.get("metrics.bind_host", "127.0.0.1"))
    )

    async with build_client(
        cfg, username="og_guardian", password=mqtt_password, client_id="og-guardian"
    ) as mqtt_client:

        async def tick() -> None:
            await write_heartbeat(pool, "guardian")
            for batch in await _fetch_pending_batches(pool):
                verdict = await service.evaluate_and_sign(batch)
                await _insert_verdict(pool, verdict)
                if verdict.outcome != "PASS":
                    continue
                proposal = await ports.proposals.fetch(batch.command_batch_id)
                if proposal is None:
                    continue
                leases.record_accepted(proposal.bank_id, proposal.epoch, proposal.seq)
                await _publish_signed_batch(
                    mqtt_client=mqtt_client,
                    cfg=cfg,
                    key_id=guardian_cfg.key_id,
                    verdict=verdict,
                    proposal=proposal,
                )

        try:
            await run_forever(tick, interval_s=guardian_cfg.cycle_interval_s, process_name="guardian")
        finally:
            telemetry_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await telemetry_task
            await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
