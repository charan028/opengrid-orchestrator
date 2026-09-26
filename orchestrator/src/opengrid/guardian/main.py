"""`og-guardian` process entry point (BUILD.md S4 process table; `python -m opengrid.guardian.main`).

Wires the real Postgres/MQTT-backed ports (`opengrid.guardian.repo`/`mqtt_io`), configures the module-
level `opengrid.guardian.evaluate_and_sign` singleton, and runs the guardian's own cycle: pick up
proposed batches that have no verdict yet, evaluate and (if PASS) publish the signed command batch and
renew the bank's hubs' leases. Heartbeats every cycle (02b S6.4).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import aiomqtt
from prometheus_client import start_http_server
from psycopg_pool import AsyncConnectionPool

import opengrid.guardian as guardian_module
from opengrid.core.models.engine import CommandBatchRow, Verdict
from opengrid.core.models.mqtt import CommandBatch, CommandItem, Lease
from opengrid.core.models.pq import CalibrationCommand, CalibrationReference
from opengrid.core.pq import CalibrationBounds
from opengrid.guardian.config import CalibrationSyncSource, GuardianConfig, load_guardian_config
from opengrid.guardian.keys import resolve_signing_seed
from opengrid.guardian.mqtt_io import (
    MqttHubStatePort,
    MqttL2InstructionPort,
    publish_calibration_command,
    publish_command_batch,
    publish_lease,
    run_telemetry_listener,
)
from opengrid.guardian.ports import ProposedBatch
from opengrid.guardian.pq_ports import FirmwareCalibrationBoundsPort, ProposedCalibrationCommand
from opengrid.guardian.repo import (
    PendingCalibration,
    PgCalibrationQueuePort,
    PgStopReleasePort,
    build_clock_port,
    build_pg_ports,
    load_bank_membership,
    load_hub_params,
)
from opengrid.guardian.service import GuardianService
from opengrid.platform.config import Config, load_config, resolve_secret
from opengrid.platform.db import POOL_OPEN_TIMEOUT_S, build_dsn
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


def build_signed_batch(service: GuardianService, *, key_id: str, proposal: ProposedBatch) -> CommandBatch:
    """The wire envelope for a PASS proposal, signed over its own fields (crypto.md S2.1) -- not the
    verdict's signature, which covers different fields and would fail every hub's BAD_SIGNATURE check."""
    unsigned = CommandBatch(
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
        signature="",
    )
    return service.sign_command_batch(unsigned)


async def _publish_signed_batch(
    *,
    mqtt_client: aiomqtt.Client,
    cfg: Config,
    service: GuardianService,
    key_id: str,
    proposal: ProposedBatch,
) -> None:
    """On a PASS verdict, publish the signed command batch and renew the bank's hub leases."""
    batch = build_signed_batch(service, key_id=key_id, proposal=proposal)
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


def build_proposed_calibration(
    pending: PendingCalibration,
    *,
    bounds: CalibrationBounds,
    now: datetime,
    lease_s: float,
    sync_source: CalibrationSyncSource,
) -> ProposedCalibrationCommand:
    """The ladder's PENDING `og.calibration_attempt` row -> the candidate G-25 evaluates (S6.7). The
    guardian supplies what the row does not carry: the lease (`issued_at` = now), its own firmware-family
    bounds, and the reference's sync source (its own time discipline, K12). `epoch`/`seq` are assigned
    only on signing, by `GuardianService.evaluate_and_sign_calibration`."""
    return ProposedCalibrationCommand(
        hub_id=pending.hub_id,
        correction=pending.correction,
        bounds=bounds,
        calibration_id=pending.calibration_id,
        reference=CalibrationReference(
            phase_deg=pending.reference_phase_deg,
            freq_hz=pending.reference_freq_hz,
            amplitude_v=pending.reference_amplitude_v,
            sync_source=sync_source,
        ),
        issued_at=now,
        expires_at=now + timedelta(seconds=lease_s),
    )


async def process_pending_calibrations(
    *,
    queue: PgCalibrationQueuePort,
    firmware_bounds: FirmwareCalibrationBoundsPort,
    service: GuardianService,
    publish: Callable[[CalibrationCommand], Awaitable[None]],
    config: GuardianConfig,
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """One pass of the calibration hand-off: evaluate every PENDING, not-yet-evaluated attempt and
    publish each command the guardian signs. Returns the number published. The guardian's own verdict
    trace (written inside `evaluate_and_sign_calibration`, before anything is published) is what claims
    the row, so a refused or published attempt is never evaluated again."""
    published = 0
    for pending in await queue.pending(max_age_s=config.calibration_max_request_age_s):
        proposed = build_proposed_calibration(
            pending,
            bounds=await firmware_bounds.max_bounds_for_hub(pending.hub_id),
            now=now_fn(),
            lease_s=config.calibration_lease_s,
            sync_source=config.calibration_sync_source,
        )
        command = await service.evaluate_and_sign_calibration(proposed)
        if command is None:
            continue
        try:
            await publish(command)
        except Exception:
            # The attempt stays PENDING with no ack; the ladder ages it out (FAILED_NO_ACK). Never retried
            # here: the signed record already exists, and a second command would need a new attempt.
            logger.exception(
                "failed to publish signed calibration command",
                extra={"calibration_id": str(command.calibration_id), "hub_id": command.hub_id},
            )
            continue
        published += 1
    return published


async def process_pending_stop_releases(
    *, port: PgStopReleasePort, service: GuardianService, config: GuardianConfig
) -> int:
    """One pass of the K8 release hand-off: decide every approved, undecided Tier-2 release request
    (`GuardianService.evaluate_and_sign_stop_release` traces each verdict, which is what claims the
    request), then hand og-safestop every signed RELEASE it has not yet published -- including ones signed
    on an earlier pass, so a restart or a missed NOTIFY never loses a release. Returns the number handed."""
    for request in await port.pending_requests(max_age_s=config.stop_release_max_age_s):
        await service.evaluate_and_sign_stop_release(request)
    events = await port.unpublished_release_events(max_age_s=config.stop_release_max_age_s)
    for event in events:
        await port.hand_to_safestop(event)
    return len(events)


async def main() -> None:
    configure_logging("guardian")
    cfg = load_config(os.environ.get("OG_CONFIG"))
    guardian_cfg = load_guardian_config(cfg)

    signing_seed = resolve_signing_seed(
        env_var_name=guardian_cfg.signing_seed_env, key_path=guardian_cfg.key_path
    )

    pool = AsyncConnectionPool(build_dsn(cfg), min_size=1, max_size=4, open=False)
    await pool.open(wait=True, timeout=POOL_OPEN_TIMEOUT_S)  # PLAT-004

    trace_store = TraceStore(PgTraceBackend(pool))
    # GUARD-02/04: guardian's hub-state read is always its OWN MQTT telemetry cache, never a Postgres
    # port -- passed in directly so `build_pg_ports` can never default to reading og.hub_state instead.
    telemetry_cache = MqttHubStatePort(
        await load_hub_params(pool),
        bank_by_hub=await load_bank_membership(pool),
        max_age_s=guardian_cfg.telemetry_max_age_s,
    )
    l2_instructions = MqttL2InstructionPort()
    release_port = PgStopReleasePort(pool)
    ports, leases = build_pg_ports(
        pool,
        trace_store,
        telemetry_cache,
        clock=build_clock_port(guardian_cfg.clock_source, cache_s=guardian_cfg.clock_cache_s),
        l2_instructions=l2_instructions,
        bank_members=telemetry_cache,
        stop_release=release_port,
    )
    calibration_queue = PgCalibrationQueuePort(pool)

    service = GuardianService(ports=ports, config=guardian_cfg, signing_seed=signing_seed)
    guardian_module.configure(service)

    mqtt_password = resolve_secret("OG_MQTT_GUARDIAN_PASSWORD")
    telemetry_task = asyncio.create_task(
        run_telemetry_listener(
            cfg,
            telemetry_cache,
            username="og_guardian",
            password=mqtt_password,
            l2_instructions=l2_instructions,
        )
    )
    start_http_server(
        int(cfg.get("metrics.guardian_port", 9103)), addr=str(cfg.get("metrics.bind_host", "127.0.0.1"))
    )

    async with build_client(
        cfg, username="og_guardian", password=mqtt_password, process="guardian"
    ) as mqtt_client:

        async def publish_calibration(command: CalibrationCommand) -> None:
            await publish_calibration_command(mqtt_client, cfg, command)

        async def tick() -> None:
            await write_heartbeat(pool, "guardian")
            for batch in await _fetch_pending_batches(pool):
                verdict = await service.evaluate_and_sign(batch)
                await _insert_verdict(pool, verdict)
                if verdict.outcome != "PASS":
                    continue
                # Publish exactly what was evaluated -- never a second read of the pre-image.
                proposal = service.evaluated_proposal(batch.command_batch_id)
                if proposal is None:
                    continue
                await leases.record_accepted(proposal.bank_id, proposal.epoch, proposal.seq)
                await _publish_signed_batch(
                    mqtt_client=mqtt_client,
                    cfg=cfg,
                    service=service,
                    key_id=guardian_cfg.key_id,
                    proposal=proposal,
                )
            if ports.pq is not None:
                # Isolated from batch signing: a calibration-path failure (e.g. og.calibration_attempt
                # not yet migrated) must never stop the guardian signing or holding dispatch (K7).
                try:
                    await process_pending_calibrations(
                        queue=calibration_queue,
                        firmware_bounds=ports.pq.firmware_bounds,
                        service=service,
                        publish=publish_calibration,
                        config=guardian_cfg,
                    )
                except Exception:
                    logger.exception("calibration hand-off pass failed; batch signing unaffected")
            # Isolated like the calibration pass: a release-path failure never touches batch signing, and a
            # stop simply stays engaged (K8 fails closed).
            try:
                await process_pending_stop_releases(port=release_port, service=service, config=guardian_cfg)
            except Exception:
                logger.exception("stop-release hand-off pass failed; stops stay engaged")

        try:
            await run_forever(tick, interval_s=guardian_cfg.cycle_interval_s, process_name="guardian")
        finally:
            telemetry_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await telemetry_task
            await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
