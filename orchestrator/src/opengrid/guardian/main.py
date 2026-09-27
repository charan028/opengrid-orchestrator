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
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from prometheus_client import start_http_server
from psycopg_pool import AsyncConnectionPool

import opengrid.guardian as guardian_module
from opengrid.core.models.engine import CommandBatchRow, Verdict
from opengrid.core.models.mqtt import CommandBatch, CommandItem, Lease
from opengrid.core.models.pq import CalibrationCommand, CalibrationReference
from opengrid.core.pq import CalibrationBounds
from opengrid.firmware.config import load_firmware_config
from opengrid.firmware.guardian_flow import PgFirmwareGuardianPort, process_pending_firmware
from opengrid.firmware.model import FirmwareCommand
from opengrid.guardian.config import CalibrationSyncSource, GuardianConfig, load_guardian_config
from opengrid.guardian.escalation import (
    CONSERVATIVE_ALERT_RULE,
    STOP_REQUEST_ALERT_RULE,
    BatchOutcome,
    EscalationTracker,
    Transition,
)
from opengrid.guardian.flow_repo import PgGridTopologyPort, PgTerritoryPort, load_required_zone_territory
from opengrid.guardian.keys import resolve_signing_seed
from opengrid.guardian.mqtt_io import (
    MqttHubStatePort,
    MqttL2InstructionPort,
    build_input_session,
    publish_calibration_command,
    publish_command_batch,
    publish_firmware_command,
    publish_lease,
)
from opengrid.guardian.ports import AlertPort, ProposedBatch, ScopePosturePort
from opengrid.guardian.pq_ports import FirmwareCalibrationBoundsPort, ProposedCalibrationCommand
from opengrid.guardian.repo import (
    ConfigMobileUnitPort,
    PendingCalibration,
    PgAlertPort,
    PgCalibrationQueuePort,
    PgScopePosturePort,
    PgStopReleasePort,
    build_clock_port,
    build_pg_ports,
    load_bank_membership,
    load_hub_params,
    load_zones_by_bank,
)
from opengrid.guardian.service import GuardianService
from opengrid.platform.config import Config, load_config, resolve_secret
from opengrid.platform.db import POOL_OPEN_TIMEOUT_S, build_dsn
from opengrid.platform.heartbeat import write_heartbeat
from opengrid.platform.log import configure_logging
from opengrid.platform.mqtt import build_client
from opengrid.platform.mqtt_session import MqttPublisher, MqttSession
from opengrid.platform.process import run_forever
from opengrid.selector.gate import load_mobile_home_station_sites, load_mobile_units
from opengrid.trace import TraceStore
from opengrid.trace.pg_backend import PgTraceBackend, journal_path_from_config

logger = logging.getLogger("guardian")

#: Fail closed on MQTT: after this long with a connection down, the process exits so systemd restarts it.
DEFAULT_MQTT_DOWN_EXIT_S = 60.0


def mqtt_inputs_ready(*sessions: MqttSession, exit_after_s: float) -> bool:
    """True when every MQTT connection is up. While one is down the guardian holds: the tick writes no
    heartbeat (health sees the guardian stale) and evaluates and signs nothing -- it never signs on a
    telemetry/L2 view it can no longer refresh, nor signs what it cannot publish. Once a connection has been
    down longer than `exit_after_s` this raises `SystemExit` so systemd restarts the process."""
    down = [s for s in sessions if not s.connected]
    if not down:
        return True
    longest_s = max(s.down_for_s() for s in down)
    names = ",".join(s.name for s in down)
    logger.error(
        "MQTT connection down: signing held", extra={"clients": names, "down_s": round(longest_s, 1)}
    )
    if longest_s > exit_after_s:
        raise SystemExit(
            f"guardian MQTT connection(s) {names} down for {longest_s:.0f}s: exiting for restart"
        )
    return False


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
    mqtt_client: MqttPublisher,
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


async def apply_escalation(
    *,
    tracker: EscalationTracker,
    posture: ScopePosturePort,
    alerts: AlertPort,
    outcomes: list[BatchOutcome],
    zone_by_bank: dict[str, str],
) -> list[Transition]:
    """ES06-S04: fold one tick's verdicts into the per-scope counter and publish the result -- the posture
    the engine honours (`og.scope_posture`), ALR-SCOPE-CONSERVATIVE while degraded, and once the escalation
    count reaches three (bad ticks without recovery, hysteresis in `escalation`) ALR-SAFE-STOP-REQUESTED plus
    an operator-action proposal. A person decides; nothing here engages a stop. Recovery clears the posture
    and both alerts."""
    transitions = tracker.observe_tick(outcomes, zone_by_bank)
    for t in transitions:
        kind, ref = t.scope
        key = f"{kind}:{ref}"
        detail: dict[str, object] = {
            "scope_kind": kind,
            "scope_ref": ref,
            "veto_ratio": round(t.veto_ratio, 4),
            "consecutive": t.consecutive,
        }
        if t.kind == "CLEAR":
            await posture.set_posture(
                kind, ref, posture="NORMAL", veto_ratio=t.veto_ratio, consecutive=0, stop_requested=False
            )
            await _clear_scope_alerts(alerts, key)
            continue
        await posture.set_posture(
            kind,
            ref,
            posture="CONSERVATIVE",
            veto_ratio=t.veto_ratio,
            consecutive=t.consecutive,
            stop_requested=tracker.stop_requested(t.scope),
        )
        if t.kind == "ENTER_CONSERVATIVE":
            await alerts.raise_alert(
                CONSERVATIVE_ALERT_RULE,
                "warning",
                f"{key} CONSERVATIVE: {t.veto_ratio:.0%} of commands vetoed",
                key,
                detail,
            )
        elif t.kind == "REQUEST_SAFE_STOP":
            reason = (
                f"{t.consecutive} CONSERVATIVE ticks without recovery ({t.veto_ratio:.0%} vetoed): "
                "safe stop requested"
            )
            await alerts.raise_alert(STOP_REQUEST_ALERT_RULE, "critical", f"{key}: {reason}", key, detail)
            await posture.propose_safe_stop(kind, ref, reason)
    touched = {f"{kind}:{ref}" for kind, ref in (t.scope for t in transitions)}
    await _reconcile_scope_alerts(tracker, posture, alerts, touched=touched)
    return transitions


async def _clear_scope_alerts(alerts: AlertPort, key: str) -> None:
    """Clear both escalation alerts of a scope; one failing never leaves the other open."""
    for rule in (CONSERVATIVE_ALERT_RULE, STOP_REQUEST_ALERT_RULE):
        try:
            await alerts.clear_alert(rule, key)
        except Exception:
            logger.exception("failed to clear escalation alert", extra={"rule": rule, "condition_key": key})


async def _reconcile_scope_alerts(
    tracker: EscalationTracker, posture: ScopePosturePort, alerts: AlertPort, *, touched: set[str]
) -> None:
    """Review fix (base, 2026-09-26: ALR-SCOPE-CONSERVATIVE alerts stayed open after their scopes returned
    to NORMAL). Every open escalation alert whose scope this guardian's tracker holds NORMAL -- a missed
    clear, or state lost across a guardian restart -- is cleared, and its posture row set back to NORMAL, so
    the alerts, `og.scope_posture` and the tracker always agree. A scope that is still bad re-enters
    CONSERVATIVE on its next bad tick. Scopes with a transition this tick were handled above."""
    for rule in (CONSERVATIVE_ALERT_RULE, STOP_REQUEST_ALERT_RULE):
        for key in await alerts.open_condition_keys(rule):
            kind, _, ref = key.partition(":")
            if key in touched or kind not in ("BANK", "ZONE") or not ref:
                continue
            if tracker.posture((kind, ref)) != "NORMAL":  # type: ignore[arg-type]
                continue
            if rule == CONSERVATIVE_ALERT_RULE:
                await posture.set_posture(
                    kind, ref, posture="NORMAL", veto_ratio=0.0, consecutive=0, stop_requested=False
                )
            await _clear_scope_alerts(alerts, key)


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

    trace_store = TraceStore(PgTraceBackend(pool, journal_path=journal_path_from_config(cfg)))
    # GUARD-02/04: guardian's hub-state read is always its OWN MQTT telemetry cache, never a Postgres
    # port -- passed in directly so `build_pg_ports` can never default to reading og.hub_state instead.
    telemetry_cache = MqttHubStatePort(
        await load_hub_params(pool),
        bank_by_hub=await load_bank_membership(pool),
        max_age_s=guardian_cfg.telemetry_max_age_s,
    )
    l2_instructions = MqttL2InstructionPort()
    release_port = PgStopReleasePort(pool)
    # K8/ES06-S04: bank -> zone, for ZONE-scope safe stops and zone veto statistics.
    zones_by_bank = await load_zones_by_bank(pool)
    alert_port = PgAlertPort(pool)
    posture_port = PgScopePosturePort(pool)
    escalation = EscalationTracker(
        conservative_ratio=guardian_cfg.escalation_conservative_ratio,
        stop_request_after=guardian_cfg.escalation_stop_request_after,
        idle_clear_ticks=guardian_cfg.escalation_idle_clear_ticks,
        clear_after_good_ticks=guardian_cfg.escalation_clear_after_good_ticks,
        clear_ratio_factor=guardian_cfg.escalation_clear_ratio_factor,
    )
    zone_territory = load_required_zone_territory()
    ports, leases = build_pg_ports(
        pool,
        trace_store,
        telemetry_cache,
        clock=build_clock_port(guardian_cfg.clock_source, cache_s=guardian_cfg.clock_cache_s),
        l2_instructions=l2_instructions,
        bank_members=telemetry_cache,
        stop_release=release_port,
        alerts=alert_port,
        zones_by_bank=zones_by_bank,
        # 09 S2.6 flow limits (G-26..G-32) and K15 territory (G-33): always wired in production.
        topology=PgGridTopologyPort(pool, guardian_cfg, zone_territory, members=telemetry_cache),
        territory=PgTerritoryPort(pool, zone_territory),
    )
    # G-35 (D-31): the mobile-unit registry, read once at start (a malformed file fails the start loudly).
    # The at-home check reads each unit's position from og.hub against its home station's coordinates.
    ports = dataclasses.replace(
        ports,
        mobile_units=ConfigMobileUnitPort(load_mobile_units(), load_mobile_home_station_sites(), pool),
    )
    calibration_queue = PgCalibrationQueuePort(pool)

    service = GuardianService(ports=ports, config=guardian_cfg, signing_seed=signing_seed)
    guardian_module.configure(service)

    mqtt_password = resolve_secret("OG_MQTT_GUARDIAN_PASSWORD")
    # Both MQTT connections survive broker disconnects (reconnect with backoff, same client id, never two
    # clients at once): the input subscription (telemetry + L2) and the signed-publish connection.
    input_session = build_input_session(
        cfg, telemetry_cache, username="og_guardian", password=mqtt_password, l2_instructions=l2_instructions
    )
    mqtt_client = MqttSession(
        lambda: build_client(cfg, username="og_guardian", password=mqtt_password, process="guardian"),
        name="guardian",
    )
    mqtt_tasks = [asyncio.create_task(input_session.run()), asyncio.create_task(mqtt_client.run())]
    mqtt_down_exit_s = float(cfg.get("guardian.mqtt_down_exit_s", DEFAULT_MQTT_DOWN_EXIT_S))
    start_http_server(
        int(cfg.get("metrics.guardian_port", 9103)), addr=str(cfg.get("metrics.bind_host", "127.0.0.1"))
    )

    async def publish_calibration(command: CalibrationCommand) -> None:
        await publish_calibration_command(mqtt_client, cfg, command)

    # R3.1 firmware updates (G-36): REQUESTED og.firmware_command rows, evaluated, signed and published here.
    fw_cfg = load_firmware_config(cfg)
    fw_port = PgFirmwareGuardianPort(pool)

    async def publish_fw(command: FirmwareCommand) -> None:
        await publish_firmware_command(mqtt_client, cfg, command)

    async def tick() -> None:
        if not mqtt_inputs_ready(input_session, mqtt_client, exit_after_s=mqtt_down_exit_s):
            return  # fail closed: no heartbeat, nothing evaluated or signed while MQTT is down
        await write_heartbeat(pool, "guardian")
        outcomes: list[BatchOutcome] = []
        for batch in await _fetch_pending_batches(pool):
            verdict = await service.evaluate_and_sign(batch)
            await _insert_verdict(pool, verdict)
            outcome = service.batch_outcome(verdict)
            if outcome is not None:
                outcomes.append(outcome)
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
        # Isolated the same way: a firmware-path failure never stops batch signing (K7).
        try:
            await process_pending_firmware(
                fw_port,
                sign=service.sign_firmware_payload,
                publish=publish_fw,
                trace=lambda s, c, p: trace_store.append(s, "GUARDIAN_VERDICT", c, p),
                cfg=fw_cfg,
                key_id=guardian_cfg.key_id,
            )
        except Exception:
            logger.exception("firmware hand-off pass failed; batch signing unaffected")
        # ES06-S04 escalation, isolated: publishing posture/alerts never touches the signing decisions.
        try:
            await apply_escalation(
                tracker=escalation,
                posture=posture_port,
                alerts=alert_port,
                outcomes=outcomes,
                zone_by_bank=zones_by_bank,
            )
        except Exception:
            logger.exception("veto escalation pass failed; signing unaffected")
        # Isolated like the calibration pass: a release-path failure never touches batch signing, and a
        # stop simply stays engaged (K8 fails closed).
        try:
            await process_pending_stop_releases(port=release_port, service=service, config=guardian_cfg)
        except Exception:
            logger.exception("stop-release hand-off pass failed; stops stay engaged")

    try:
        await run_forever(tick, interval_s=guardian_cfg.cycle_interval_s, process_name="guardian")
    finally:
        for task in mqtt_tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
