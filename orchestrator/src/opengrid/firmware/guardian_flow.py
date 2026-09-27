"""The guardian side of the firmware hand-off (R3.1, K3/K6/K10). Runs INSIDE og-guardian (it holds the key):

    await process_pending_firmware(port, sign=..., publish=..., trace=..., cfg=..., key_id=..., catalogue_entries=...)

For every REQUESTED `og.firmware_command` row (written by the engine's executor) it reads the hub's facts
itself (`PgFirmwareGuardianPort`: og.hub / og.hub_state / og.stop_event / og.reservation and its own signed
ledger for in-flight counts -- never the engine's plan), runs G-36 (`check_firmware_eligibility`), and
either REFUSES the row (reason traced) or reserves the hub's next `(epoch, seq)`, signs the full wire envelope
(`FirmwareCommand.signing_payload()`), traces the signed verdict and marks the row SIGNED -- in that order,
and only then publishes it on `<root>/cmd/fw/<hub_id>` (K10: nothing is released before its verdict is
durable). A row is claimed exactly once; a retry is a new row with a fresh `(epoch, seq)`.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol
from uuid import UUID

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from opengrid.firmware.catalogue import Catalogue
from opengrid.firmware.config import FirmwareConfig
from opengrid.firmware.model import FirmwareCommand
from opengrid.guardian.firmware_check import (
    FirmwareCampaignGrant,
    FirmwareHubFacts,
    ProposedFirmwareCommand,
    check_firmware_eligibility,
)

logger = logging.getLogger(__name__)

#: Firmware commands use their own epoch; seq increases strictly per hub within it (K6).
FIRMWARE_EPOCH: Final = 1
_ONLINE_MAX_AGE_S: Final = 30.0


@dataclass(frozen=True, slots=True)
class PendingFirmwareCommand:
    command_id: UUID
    job_id: UUID
    campaign_id: UUID
    hub_id: str
    attempt: int
    action: str
    target_version: str
    from_version: str | None
    sha256: str
    hardware_revision: str
    issued_at: datetime
    expires_at: datetime


class FirmwareGuardianPort(Protocol):
    async def pending(self) -> list[PendingFirmwareCommand]: ...
    async def grant(self, campaign_id: UUID) -> FirmwareCampaignGrant | None: ...
    async def hub_facts(
        self, hub_id: str, *, lookahead_s: float, update_timeout_s: float
    ) -> FirmwareHubFacts | None: ...
    async def catalogue_rows(self) -> list[dict[str, Any]]: ...
    async def reserve(self, command_id: UUID, hub_id: str) -> tuple[int, int] | None: ...
    async def mark_signed(self, command_id: UUID, payload: dict[str, Any]) -> None: ...
    async def refuse(self, command_id: UUID, reason: str) -> None: ...
    async def mark_published(self, command_id: UUID) -> None: ...


Signer = Callable[[dict[str, Any]], str]
Publisher = Callable[[FirmwareCommand], Awaitable[None]]
VerdictTrace = Callable[[str, str, dict[str, Any]], Awaitable[object]]


async def process_pending_firmware(
    port: FirmwareGuardianPort,
    *,
    sign: Signer,
    publish: Publisher,
    trace: VerdictTrace,
    cfg: FirmwareConfig,
    key_id: str,
    catalogue_entries: Sequence[Mapping[str, Any]] = (),
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """One pass over REQUESTED firmware commands. Returns how many were signed and published."""
    pending = await port.pending()
    if not pending:
        return 0
    catalogue = Catalogue.build(catalogue_entries or cfg.catalogue, await port.catalogue_rows())
    published = 0
    for request in pending:
        try:
            if await _evaluate_one(port, request, catalogue, sign, publish, trace, cfg, key_id, now_fn()):
                published += 1
        except Exception:
            logger.exception(
                "firmware command evaluation failed", extra={"command_id": str(request.command_id)}
            )
    return published


async def _evaluate_one(
    port: FirmwareGuardianPort,
    request: PendingFirmwareCommand,
    catalogue: Catalogue,
    sign: Signer,
    publish: Publisher,
    trace: VerdictTrace,
    cfg: FirmwareConfig,
    key_id: str,
    now: datetime,
) -> bool:
    grant = await port.grant(request.campaign_id)
    facts = await port.hub_facts(
        request.hub_id, lookahead_s=cfg.committed_lookahead_s, update_timeout_s=cfg.update_timeout_s
    )
    if grant is None or facts is None:
        await _refuse(port, trace, request, "FIRMWARE_UNKNOWN_CAMPAIGN_OR_HUB")
        return False
    outcome = check_firmware_eligibility(
        ProposedFirmwareCommand(
            hub_id=request.hub_id,
            action=request.action,
            target_version=request.target_version,
            sha256=request.sha256,
            hardware_revision=request.hardware_revision,
            issued_at=request.issued_at,
            expires_at=request.expires_at,
        ),
        facts,
        grant,
        catalogue,
        now=now,
        soc_margin_pct=cfg.soc_margin_pct,
        max_lease_s=cfg.command_lease_s,
        max_issue_skew_s=cfg.max_issue_skew_s,
    )
    if not outcome.ok:
        await _refuse(port, trace, request, outcome.reason or outcome.rule_id)
        return False

    sequence = await port.reserve(request.command_id, request.hub_id)
    if sequence is None:
        logger.info(
            "firmware command already claimed; not signing it again",
            extra={"command_id": str(request.command_id)},
        )
        return False
    epoch, seq = sequence
    unsigned = FirmwareCommand(
        command_id=request.command_id,
        campaign_id=request.campaign_id,
        job_id=request.job_id,
        hub_id=request.hub_id,
        action="ROLLBACK" if request.action == "ROLLBACK" else "UPDATE",
        target_version=request.target_version,
        from_version=request.from_version,
        sha256=request.sha256,
        hardware_revision=request.hardware_revision,
        attempt=request.attempt,
        epoch=epoch,
        seq=seq,
        issued_at=request.issued_at,
        expires_at=request.expires_at,
        key_id=key_id,
        signature="",
    )
    command = unsigned.model_copy(update={"signature": sign(unsigned.signing_payload())})
    wire = command.model_dump(mode="json")
    try:
        await trace(
            f"guardian_firmware:{request.hub_id}",
            "FIRMWARE_UPDATE",
            {"kind": "FIRMWARE_UPDATE", "outcome": "SIGNED", "rule_id": outcome.rule_id, "command": wire},
        )
        await port.mark_signed(request.command_id, wire)
    except Exception:
        # K10: withhold. The reserved (epoch, seq) is simply never used; the engine re-requests after the lease.
        logger.exception(
            "failed to record signed firmware command; withholding it",
            extra={"command_id": str(request.command_id)},
        )
        return False
    await publish(command)
    await port.mark_published(request.command_id)
    return True


async def _refuse(
    port: FirmwareGuardianPort, trace: VerdictTrace, request: PendingFirmwareCommand, reason: str
) -> None:
    await port.refuse(request.command_id, reason)
    try:
        await trace(
            f"guardian_firmware:{request.hub_id}",
            "FIRMWARE_UPDATE",
            {
                "kind": "FIRMWARE_UPDATE",
                "outcome": "REFUSED",
                "command_id": str(request.command_id),
                "hub_id": request.hub_id,
                "reason": reason,
            },
        )
    except Exception:
        logger.exception("failed to trace refused firmware command")


# ============================================================================================== Postgres

_PENDING_SQL = """
SELECT command_id, job_id, campaign_id, hub_id, attempt, action, target_version, from_version, sha256,
       hardware_revision, issued_at, expires_at
FROM og.firmware_command WHERE status = 'REQUESTED' AND expires_at > now()
ORDER BY requested_at LIMIT 200
"""

_GRANT_SQL = """
SELECT state, allow_downgrade, override_committed,
       (approved_by IS NOT NULL AND lower(approved_by) <> lower(proposed_by)) AS second_operator,
       bank_max_concurrent_pct, feeder_max_concurrent_pct
FROM og.firmware_campaign WHERE campaign_id = %(id)s
"""

#: In flight per the guardian's OWN signed ledger: signed, not finished, not stale.
_IN_FLIGHT = """
    c.status IN ('SIGNED', 'PUBLISHED')
    AND (c.hub_state IS NULL OR c.hub_state IN ('ACCEPTED', 'DOWNLOADING', 'INSTALLING', 'REBOOTING'))
    AND c.signed_at > now() - make_interval(secs => %(update_timeout_s)s)
    AND (c.hub_state IS NOT NULL OR c.expires_at > now())
"""

_FACTS_SQL = f"""
WITH hub AS (
    SELECT h.hub_id, h.bank_id, h.zone, b.feeder_id, h.e_kwh, h.r_kwh, h.firmware_version, h.hardware_revision
    FROM og.hub h LEFT JOIN og.bank b ON b.bank_id = h.bank_id WHERE h.hub_id = %(hub_id)s
), latest_stop AS (
    SELECT DISTINCT ON (scope_kind, scope_ref) scope_kind, scope_ref, action
    FROM og.stop_event ORDER BY scope_kind, scope_ref, created_at DESC
)
SELECT hub.hub_id, hub.e_kwh, hub.r_kwh, hub.firmware_version, hub.hardware_revision, s.soc_kwh,
       (s.health = 'online' AND s.last_seen_at > now() - make_interval(secs => %(online_s)s)) AS online,
       EXISTS (SELECT 1 FROM latest_stop l WHERE l.action = 'ENGAGE' AND (l.scope_kind = 'FLEET'
               OR (l.scope_kind = 'BANK' AND l.scope_ref = hub.bank_id)
               OR (l.scope_kind = 'ZONE' AND l.scope_ref = hub.zone))) AS stopped,
       EXISTS (SELECT 1 FROM og.firmware_command c WHERE c.hub_id = hub.hub_id AND {_IN_FLIGHT}) AS updating,
       EXISTS (SELECT 1 FROM og.reservation r JOIN og.obligation o ON o.obligation_id = r.obligation_id
               WHERE r.bank_id::text = hub.bank_id AND r.released_at IS NULL
                 AND r.interval_start < now() + make_interval(secs => %(lookahead_s)s)
                 AND r.interval_end > now() AND o.state IN ('COMMITTED', 'DELIVERING')) AS committed,
       (SELECT count(*) FROM og.firmware_command c JOIN og.hub h2 ON h2.hub_id = c.hub_id
         WHERE h2.bank_id = hub.bank_id AND {_IN_FLIGHT})::int AS bank_in_flight,
       (SELECT count(*) FROM og.hub h2 WHERE h2.bank_id = hub.bank_id)::int AS bank_hubs,
       (SELECT count(*) FROM og.firmware_command c JOIN og.hub h2 ON h2.hub_id = c.hub_id
         JOIN og.bank b2 ON b2.bank_id = h2.bank_id
         WHERE b2.feeder_id = hub.feeder_id AND {_IN_FLIGHT})::int AS feeder_in_flight,
       (SELECT count(*) FROM og.hub h2 JOIN og.bank b2 ON b2.bank_id = h2.bank_id
         WHERE b2.feeder_id = hub.feeder_id)::int AS feeder_hubs
FROM hub LEFT JOIN og.hub_state s ON s.hub_id = hub.hub_id
"""  # noqa: S608 -- constant fragments; values are bound parameters

_RESERVE_SQL = """
UPDATE og.firmware_command SET status = 'RESERVED', epoch = %(epoch)s,
    seq = (SELECT COALESCE(MAX(seq), 0) + 1 FROM og.firmware_command WHERE hub_id = %(hub_id)s AND epoch = %(epoch)s)
WHERE command_id = %(command_id)s AND status = 'REQUESTED'
RETURNING epoch, seq
"""


class PgFirmwareGuardianPort:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def pending(self) -> list[PendingFirmwareCommand]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_PENDING_SQL)
            return [PendingFirmwareCommand(*row) for row in await cur.fetchall()]

    async def grant(self, campaign_id: UUID) -> FirmwareCampaignGrant | None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_GRANT_SQL, {"id": campaign_id})
            row = await cur.fetchone()
        if row is None:
            return None
        return FirmwareCampaignGrant(
            campaign_state=str(row[0]),
            allow_downgrade=bool(row[1]),
            override_committed=bool(row[2]),
            override_second_operator=bool(row[3]),
            bank_max_concurrent_pct=float(row[4]),
            feeder_max_concurrent_pct=float(row[5]),
        )

    async def hub_facts(
        self, hub_id: str, *, lookahead_s: float, update_timeout_s: float
    ) -> FirmwareHubFacts | None:
        params = {
            "hub_id": hub_id,
            "lookahead_s": lookahead_s,
            "update_timeout_s": update_timeout_s,
            "online_s": _ONLINE_MAX_AGE_S,
        }
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_FACTS_SQL, params)
            row = await cur.fetchone()
        if row is None:
            return None
        (hub, e_kwh, r_kwh, version, revision, soc, online, stopped, updating, committed,
         bank_flight, bank_hubs, feeder_flight, feeder_hubs) = row  # fmt: skip
        return FirmwareHubFacts(
            hub_id=str(hub),
            online=bool(online),
            safe_stopped=bool(stopped),
            soc_kwh=float(soc) if soc is not None else None,
            reserve_kwh=float(r_kwh),
            capacity_kwh=float(e_kwh),
            already_updating=bool(updating),
            hardware_revision=revision,
            current_version=version,
            committed_in_window=bool(committed),
            bank_in_flight=int(bank_flight),
            bank_hub_count=int(bank_hubs),
            feeder_in_flight=int(feeder_flight),
            feeder_hub_count=int(feeder_hubs),
        )

    async def catalogue_rows(self) -> list[dict[str, Any]]:
        from opengrid.firmware.repo import PgFirmwareRepo

        return await PgFirmwareRepo(self._pool).catalogue_rows()

    async def reserve(self, command_id: UUID, hub_id: str) -> tuple[int, int] | None:
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            # One reservation per hub at a time: serialise on the hub row (the calibration ledger pattern).
            await cur.execute("SELECT 1 FROM og.hub WHERE hub_id = %(hub_id)s FOR UPDATE", {"hub_id": hub_id})
            await cur.execute(
                _RESERVE_SQL, {"command_id": command_id, "hub_id": hub_id, "epoch": FIRMWARE_EPOCH}
            )
            row = await cur.fetchone()
        return (int(row[0]), int(row[1])) if row else None

    async def mark_signed(self, command_id: UUID, payload: dict[str, Any]) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE og.firmware_command SET status = 'SIGNED', signed_at = now(), payload = %(payload)s "
                "WHERE command_id = %(id)s AND status = 'RESERVED'",
                {"id": command_id, "payload": Jsonb(payload)},
            )
            await conn.commit()

    async def refuse(self, command_id: UUID, reason: str) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE og.firmware_command SET status = 'REFUSED', refuse_reason = %(reason)s "
                "WHERE command_id = %(id)s AND status = 'REQUESTED'",
                {"id": command_id, "reason": reason},
            )
            await conn.commit()

    async def mark_published(self, command_id: UUID) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE og.firmware_command SET status = 'PUBLISHED', published_at = now() "
                "WHERE command_id = %(id)s AND status = 'SIGNED'",
                {"id": command_id},
            )
            await conn.commit()
