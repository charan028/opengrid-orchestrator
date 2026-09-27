"""Persistence for firmware campaigns (migration 0037). `FirmwareRepo` is the port the API and the executor
use; `PgFirmwareRepo` is its Postgres implementation (unit tests use an in-memory fake). The guardian side
of the hand-off has its own reads (`opengrid.firmware.guardian_flow.PgFirmwareGuardianPort`).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from opengrid.firmware.model import (
    Campaign,
    CampaignState,
    CommandRecord,
    Job,
    JobEvent,
    JobState,
)
from opengrid.firmware.planner import FleetCounts


@dataclass(frozen=True, slots=True)
class HubSelection:
    """Explicit ids, or a filter (every given field must match; an empty filter selects nothing)."""

    hub_ids: tuple[str, ...] = ()
    bank_ids: tuple[str, ...] = ()
    zones: tuple[str, ...] = ()
    feeder_ids: tuple[str, ...] = ()
    hardware_revisions: tuple[str, ...] = ()
    firmware_versions: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not any(
            (
                self.hub_ids,
                self.bank_ids,
                self.zones,
                self.feeder_ids,
                self.hardware_revisions,
                self.firmware_versions,
            )
        )

    def as_json(self) -> dict[str, list[str]]:
        return {k: list(getattr(self, k)) for k in self.__slots__ if getattr(self, k)}


@dataclass(frozen=True, slots=True)
class HubRow:
    hub_id: str
    bank_id: str
    zone: str
    feeder_id: str | None
    firmware_version: str | None
    hardware_revision: str | None


@dataclass(frozen=True, slots=True)
class CommandRequest:
    job: Job
    sha256: str
    hardware_revision: str
    issued_at: datetime
    expires_at: datetime
    command_id: UUID = field(default_factory=uuid4)


class FirmwareRepo(Protocol):
    async def catalogue_rows(self) -> list[dict[str, Any]]: ...
    async def resolve_hubs(self, selection: HubSelection) -> list[HubRow]: ...
    async def committed_hub_ids(self, hub_ids: Sequence[str], *, until: datetime) -> set[str]: ...
    async def fleet_counts(self) -> FleetCounts: ...
    async def create_campaign(
        self, campaign: Campaign, jobs: Sequence[Job], events: Sequence[JobEvent]
    ) -> None: ...
    async def get_campaign(self, campaign_id: UUID) -> Campaign | None: ...
    async def list_campaigns(self, *, limit: int = 50) -> list[Campaign]: ...
    async def active_campaigns(self) -> list[Campaign]: ...
    async def jobs(self, campaign_id: UUID) -> list[Job]: ...
    async def events(
        self, campaign_id: UUID, *, after_id: int = 0, limit: int = 500
    ) -> list[tuple[int, JobEvent]]: ...
    async def save_campaign(self, campaign: Campaign, events: Sequence[JobEvent]) -> None: ...
    async def save_jobs(self, jobs: Sequence[Job], events: Sequence[JobEvent]) -> None: ...
    async def request_commands(self, requests: Sequence[CommandRequest]) -> None: ...
    async def commands(self, command_ids: Iterable[UUID]) -> dict[UUID, CommandRecord]: ...
    async def hub_versions(self, hub_ids: Iterable[str]) -> dict[str, str | None]: ...


# ============================================================================================== Postgres

_HUBS_SQL = """
SELECT h.hub_id, h.bank_id, h.zone, b.feeder_id, h.firmware_version, h.hardware_revision
FROM og.hub h LEFT JOIN og.bank b ON b.bank_id = h.bank_id
WHERE (cardinality(%(hub_ids)s::text[]) = 0 OR h.hub_id = ANY(%(hub_ids)s))
  AND (cardinality(%(bank_ids)s::text[]) = 0 OR h.bank_id = ANY(%(bank_ids)s))
  AND (cardinality(%(zones)s::text[]) = 0 OR h.zone = ANY(%(zones)s))
  AND (cardinality(%(feeder_ids)s::text[]) = 0 OR b.feeder_id = ANY(%(feeder_ids)s))
  AND (cardinality(%(revisions)s::text[]) = 0 OR h.hardware_revision = ANY(%(revisions)s))
  AND (cardinality(%(versions)s::text[]) = 0 OR h.firmware_version = ANY(%(versions)s))
ORDER BY h.hub_id
"""

#: A hub "serves a committed obligation in the window" when its bank holds an unreleased reservation of a
#: COMMITTED/DELIVERING obligation overlapping [now, until) -- the same bank-level notion fleet_bulk uses.
_COMMITTED_SQL = """
SELECT DISTINCT h.hub_id
FROM og.hub h
JOIN og.reservation r ON r.bank_id::text = h.bank_id
JOIN og.obligation o ON o.obligation_id = r.obligation_id
WHERE h.hub_id = ANY(%(hub_ids)s)
  AND r.released_at IS NULL AND r.interval_start < %(until)s AND r.interval_end > now()
  AND o.state IN ('COMMITTED', 'DELIVERING')
"""

_COUNTS_SQL = """
SELECT h.bank_id, b.feeder_id, count(*)::int
FROM og.hub h LEFT JOIN og.bank b ON b.bank_id = h.bank_id
GROUP BY h.bank_id, b.feeder_id
"""

_IN_FLIGHT_SQL = """
SELECT j.bank_id, j.feeder_id, count(*)::int
FROM og.firmware_job j JOIN og.firmware_campaign c ON c.campaign_id = j.campaign_id
WHERE (j.state IN ('SENT', 'UPDATING') OR (j.state = 'PENDING' AND j.command_id IS NOT NULL))
  AND c.state <> 'ABORTED'
GROUP BY j.bank_id, j.feeder_id
"""

_CAMPAIGN_COLUMNS = (
    "campaign_id, name, target_version, state, selection, hub_ids, waves, bank_max_concurrent_pct, "
    "feeder_max_concurrent_pct, max_failures, max_failure_pct, window_start, window_end, allow_downgrade, "
    "override_committed, requires_second_operator, second_operator_reasons, reason, proposed_by, confirmed_by, "
    "approved_by, halt_reason, created_at, confirmed_at, approved_at, started_at, finished_at, trace_id"
)

_INSERT_CAMPAIGN_SQL = f"""
INSERT INTO og.firmware_campaign ({_CAMPAIGN_COLUMNS})
VALUES (%(campaign_id)s, %(name)s, %(target_version)s, %(state)s, %(selection)s, %(hub_ids)s, %(waves)s,
        %(bank_max_concurrent_pct)s, %(feeder_max_concurrent_pct)s, %(max_failures)s, %(max_failure_pct)s,
        %(window_start)s, %(window_end)s, %(allow_downgrade)s, %(override_committed)s,
        %(requires_second_operator)s, %(second_operator_reasons)s, %(reason)s, %(proposed_by)s,
        %(confirmed_by)s, %(approved_by)s, %(halt_reason)s, %(created_at)s, %(confirmed_at)s, %(approved_at)s,
        %(started_at)s, %(finished_at)s, %(trace_id)s)
"""  # noqa: S608 -- constant column list; values are bound parameters

_UPDATE_CAMPAIGN_SQL = """
UPDATE og.firmware_campaign SET state = %(state)s, confirmed_by = %(confirmed_by)s, approved_by = %(approved_by)s,
    halt_reason = %(halt_reason)s, confirmed_at = %(confirmed_at)s, approved_at = %(approved_at)s,
    started_at = %(started_at)s, finished_at = %(finished_at)s, trace_id = %(trace_id)s, updated_at = now()
WHERE campaign_id = %(campaign_id)s
"""

_JOB_COLUMNS = (
    "job_id, campaign_id, hub_id, bank_id, feeder_id, wave, action, state, from_version, target_version, "
    "attempts, next_attempt_at, command_id, reason, terminal_failure, sent_at, updating_at, finished_at"
)

_INSERT_JOB_SQL = f"""
INSERT INTO og.firmware_job ({_JOB_COLUMNS})
VALUES (%(job_id)s, %(campaign_id)s, %(hub_id)s, %(bank_id)s, %(feeder_id)s, %(wave)s, %(action)s, %(state)s,
        %(from_version)s, %(target_version)s, %(attempts)s, %(next_attempt_at)s, %(command_id)s, %(reason)s,
        %(terminal_failure)s, %(sent_at)s, %(updating_at)s, %(finished_at)s)
"""  # noqa: S608 -- constant column list; values are bound parameters

_UPDATE_JOB_SQL = """
UPDATE og.firmware_job SET action = %(action)s, state = %(state)s, target_version = %(target_version)s,
    attempts = %(attempts)s, next_attempt_at = %(next_attempt_at)s, command_id = %(command_id)s,
    reason = %(reason)s, terminal_failure = %(terminal_failure)s, sent_at = %(sent_at)s,
    updating_at = %(updating_at)s, finished_at = %(finished_at)s, updated_at = now()
WHERE job_id = %(job_id)s
"""

_INSERT_EVENT_SQL = """
INSERT INTO og.firmware_job_event (campaign_id, job_id, hub_id, event, from_state, to_state, reason, attempt,
    detail, ts)
VALUES (%(campaign_id)s, %(job_id)s, %(hub_id)s, %(event)s, %(from_state)s, %(to_state)s, %(reason)s,
    %(attempt)s, %(detail)s, %(ts)s)
"""

_INSERT_COMMAND_SQL = """
INSERT INTO og.firmware_command (command_id, job_id, campaign_id, hub_id, attempt, action, target_version,
    from_version, sha256, hardware_revision, issued_at, expires_at)
VALUES (%(command_id)s, %(job_id)s, %(campaign_id)s, %(hub_id)s, %(attempt)s, %(action)s, %(target_version)s,
    %(from_version)s, %(sha256)s, %(hardware_revision)s, %(issued_at)s, %(expires_at)s)
ON CONFLICT (command_id) DO NOTHING
"""

_COMMANDS_SQL = """
SELECT command_id, job_id, hub_id, attempt, status, expires_at, refuse_reason, hub_state, hub_reason,
       hub_version, hub_ts
FROM og.firmware_command WHERE command_id = ANY(%(ids)s)
"""


def _campaign_params(c: Campaign) -> dict[str, Any]:
    return {
        "campaign_id": c.campaign_id,
        "name": c.name,
        "target_version": c.target_version,
        "state": c.state.value,
        "selection": Jsonb(c.selection),
        "hub_ids": list(c.hub_ids),
        "waves": list(c.waves),
        "bank_max_concurrent_pct": c.bank_max_concurrent_pct,
        "feeder_max_concurrent_pct": c.feeder_max_concurrent_pct,
        "max_failures": c.max_failures,
        "max_failure_pct": c.max_failure_pct,
        "window_start": c.window_start,
        "window_end": c.window_end,
        "allow_downgrade": c.allow_downgrade,
        "override_committed": c.override_committed,
        "requires_second_operator": c.requires_second_operator,
        "second_operator_reasons": list(c.second_operator_reasons),
        "reason": c.reason,
        "proposed_by": c.proposed_by,
        "confirmed_by": c.confirmed_by,
        "approved_by": c.approved_by,
        "halt_reason": c.halt_reason,
        "created_at": c.created_at,
        "confirmed_at": c.confirmed_at,
        "approved_at": c.approved_at,
        "started_at": c.started_at,
        "finished_at": c.finished_at,
        "trace_id": c.trace_id,
    }


def _campaign_from_row(row: Sequence[Any]) -> Campaign:
    (
        campaign_id, name, target_version, state, selection, hub_ids, waves, bank_pct, feeder_pct, max_failures,
        max_failure_pct, window_start, window_end, allow_downgrade, override_committed, requires_second,
        second_reasons, reason, proposed_by, confirmed_by, approved_by, halt_reason, created_at, confirmed_at,
        approved_at, started_at, finished_at, trace_id,
    ) = row  # fmt: skip
    return Campaign(
        campaign_id=campaign_id,
        name=name,
        target_version=target_version,
        state=CampaignState(state),
        hub_ids=list(hub_ids),
        waves=list(waves),
        reason=reason,
        proposed_by=proposed_by,
        created_at=created_at,
        selection=dict(selection or {}),
        bank_max_concurrent_pct=float(bank_pct),
        feeder_max_concurrent_pct=float(feeder_pct),
        max_failures=int(max_failures),
        max_failure_pct=float(max_failure_pct),
        window_start=window_start,
        window_end=window_end,
        allow_downgrade=bool(allow_downgrade),
        override_committed=bool(override_committed),
        requires_second_operator=bool(requires_second),
        second_operator_reasons=list(second_reasons or []),
        confirmed_by=confirmed_by,
        approved_by=approved_by,
        halt_reason=halt_reason,
        confirmed_at=confirmed_at,
        approved_at=approved_at,
        started_at=started_at,
        finished_at=finished_at,
        trace_id=trace_id,
    )


def _job_params(j: Job) -> dict[str, Any]:
    return {
        "job_id": j.job_id,
        "campaign_id": j.campaign_id,
        "hub_id": j.hub_id,
        "bank_id": j.bank_id,
        "feeder_id": j.feeder_id,
        "wave": j.wave,
        "action": j.action,
        "state": j.state.value,
        "from_version": j.from_version,
        "target_version": j.target_version,
        "attempts": j.attempts,
        "next_attempt_at": j.next_attempt_at,
        "command_id": j.command_id,
        "reason": j.reason,
        "terminal_failure": j.terminal_failure,
        "sent_at": j.sent_at,
        "updating_at": j.updating_at,
        "finished_at": j.finished_at,
    }


def _job_from_row(row: Sequence[Any]) -> Job:
    (
        job_id, campaign_id, hub_id, bank_id, feeder_id, wave, action, state, from_version, target_version,
        attempts, next_attempt_at, command_id, reason, terminal_failure, sent_at, updating_at, finished_at,
    ) = row  # fmt: skip
    return Job(
        job_id=job_id,
        campaign_id=campaign_id,
        hub_id=hub_id,
        bank_id=bank_id,
        wave=int(wave),
        target_version=target_version,
        state=JobState(state),
        feeder_id=feeder_id,
        action="ROLLBACK" if action == "ROLLBACK" else "UPDATE",
        from_version=from_version,
        attempts=int(attempts),
        next_attempt_at=next_attempt_at,
        command_id=command_id,
        reason=reason,
        terminal_failure=bool(terminal_failure),
        sent_at=sent_at,
        updating_at=updating_at,
        finished_at=finished_at,
    )


def _event_params(e: JobEvent) -> dict[str, Any]:
    return {
        "campaign_id": e.campaign_id,
        "job_id": e.job_id,
        "hub_id": e.hub_id,
        "event": e.event,
        "from_state": e.from_state,
        "to_state": e.to_state,
        "reason": e.reason,
        "attempt": e.attempt,
        "detail": Jsonb(e.detail) if e.detail else None,
        "ts": e.ts,
    }


class PgFirmwareRepo:
    """Postgres `FirmwareRepo`. Every multi-row write is one transaction (a job update and its events land
    together or not at all)."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def catalogue_rows(self) -> list[dict[str, Any]]:
        sql = (
            "SELECT version, hardware_revision, sha256, release_note, released_at::text "
            "FROM og.firmware_catalogue WHERE withdrawn_at IS NULL"
        )
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql)
            rows = await cur.fetchall()
        keys = ("version", "hardware_revision", "sha256", "release_note", "released_at")
        return [dict(zip(keys, row, strict=True)) for row in rows]

    async def resolve_hubs(self, selection: HubSelection) -> list[HubRow]:
        if selection.is_empty():
            return []
        params = {
            "hub_ids": list(selection.hub_ids),
            "bank_ids": list(selection.bank_ids),
            "zones": list(selection.zones),
            "feeder_ids": list(selection.feeder_ids),
            "revisions": list(selection.hardware_revisions),
            "versions": list(selection.firmware_versions),
        }
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_HUBS_SQL, params)
            rows = await cur.fetchall()
        return [HubRow(*row) for row in rows]

    async def committed_hub_ids(self, hub_ids: Sequence[str], *, until: datetime) -> set[str]:
        if not hub_ids:
            return set()
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_COMMITTED_SQL, {"hub_ids": list(hub_ids), "until": until})
            return {str(row[0]) for row in await cur.fetchall()}

    async def fleet_counts(self) -> FleetCounts:
        bank_hubs: dict[str, int] = {}
        feeder_hubs: dict[str, int] = {}
        bank_flight: dict[str, int] = {}
        feeder_flight: dict[str, int] = {}
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_COUNTS_SQL)
            for bank_id, feeder_id, n in await cur.fetchall():
                bank_hubs[bank_id] = bank_hubs.get(bank_id, 0) + n
                if feeder_id is not None:
                    feeder_hubs[feeder_id] = feeder_hubs.get(feeder_id, 0) + n
            await cur.execute(_IN_FLIGHT_SQL)
            for bank_id, feeder_id, n in await cur.fetchall():
                bank_flight[bank_id] = bank_flight.get(bank_id, 0) + n
                if feeder_id is not None:
                    feeder_flight[feeder_id] = feeder_flight.get(feeder_id, 0) + n
        return FleetCounts(bank_hubs, feeder_hubs, bank_flight, feeder_flight)

    async def create_campaign(
        self, campaign: Campaign, jobs: Sequence[Job], events: Sequence[JobEvent]
    ) -> None:
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute(_INSERT_CAMPAIGN_SQL, _campaign_params(campaign))
            if jobs:
                await cur.executemany(_INSERT_JOB_SQL, [_job_params(j) for j in jobs])
            if events:
                await cur.executemany(_INSERT_EVENT_SQL, [_event_params(e) for e in events])

    async def _campaigns(self, where: str, params: dict[str, Any]) -> list[Campaign]:
        sql = f"SELECT {_CAMPAIGN_COLUMNS} FROM og.firmware_campaign {where}"  # noqa: S608 -- constant fragments
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)
            return [_campaign_from_row(row) for row in await cur.fetchall()]

    async def get_campaign(self, campaign_id: UUID) -> Campaign | None:
        found = await self._campaigns("WHERE campaign_id = %(id)s", {"id": campaign_id})
        return found[0] if found else None

    async def list_campaigns(self, *, limit: int = 50) -> list[Campaign]:
        return await self._campaigns("ORDER BY created_at DESC LIMIT %(limit)s", {"limit": limit})

    async def active_campaigns(self) -> list[Campaign]:
        where = (
            "WHERE state IN ('APPROVED', 'RUNNING', 'PAUSED', 'HALTED') OR (state = 'COMPLETED' AND EXISTS ("
            "SELECT 1 FROM og.firmware_job j WHERE j.campaign_id = og.firmware_campaign.campaign_id "
            "AND j.state IN ('PENDING', 'SENT', 'UPDATING'))) ORDER BY created_at"
        )
        return await self._campaigns(where, {})

    async def jobs(self, campaign_id: UUID) -> list[Job]:
        sql = f"SELECT {_JOB_COLUMNS} FROM og.firmware_job WHERE campaign_id = %(id)s ORDER BY wave, hub_id"  # noqa: S608
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, {"id": campaign_id})
            return [_job_from_row(row) for row in await cur.fetchall()]

    async def events(
        self, campaign_id: UUID, *, after_id: int = 0, limit: int = 500
    ) -> list[tuple[int, JobEvent]]:
        sql = (
            "SELECT event_id, campaign_id, job_id, hub_id, event, from_state, to_state, reason, attempt, detail, ts "
            "FROM og.firmware_job_event WHERE campaign_id = %(id)s AND event_id > %(after)s "
            "ORDER BY event_id LIMIT %(limit)s"
        )
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, {"id": campaign_id, "after": after_id, "limit": limit})
            rows = await cur.fetchall()
        return [
            (
                int(r[0]),
                JobEvent(
                    campaign_id=r[1],
                    job_id=r[2],
                    hub_id=r[3],
                    event=r[4],
                    from_state=r[5],
                    to_state=r[6],
                    reason=r[7],
                    attempt=r[8],
                    detail=r[9],
                    ts=r[10],
                ),
            )
            for r in rows
        ]

    async def save_campaign(self, campaign: Campaign, events: Sequence[JobEvent]) -> None:
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute(_UPDATE_CAMPAIGN_SQL, _campaign_params(campaign))
            if events:
                await cur.executemany(_INSERT_EVENT_SQL, [_event_params(e) for e in events])

    async def save_jobs(self, jobs: Sequence[Job], events: Sequence[JobEvent]) -> None:
        if not jobs and not events:
            return
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            if jobs:
                await cur.executemany(_UPDATE_JOB_SQL, [_job_params(j) for j in jobs])
            if events:
                await cur.executemany(_INSERT_EVENT_SQL, [_event_params(e) for e in events])

    async def request_commands(self, requests: Sequence[CommandRequest]) -> None:
        """Insert one REQUESTED command per job and point the job at it, atomically."""
        if not requests:
            return
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.executemany(
                _INSERT_COMMAND_SQL,
                [
                    {
                        "command_id": r.command_id,
                        "job_id": r.job.job_id,
                        "campaign_id": r.job.campaign_id,
                        "hub_id": r.job.hub_id,
                        "attempt": r.job.attempts + 1,
                        "action": r.job.action,
                        "target_version": r.job.target_version,
                        "from_version": r.job.from_version,
                        "sha256": r.sha256,
                        "hardware_revision": r.hardware_revision,
                        "issued_at": r.issued_at,
                        "expires_at": r.expires_at,
                    }
                    for r in requests
                ],
            )
            await cur.executemany(
                "UPDATE og.firmware_job SET command_id = %(command_id)s, updated_at = now() WHERE job_id = %(job_id)s",
                [{"command_id": r.command_id, "job_id": r.job.job_id} for r in requests],
            )

    async def commands(self, command_ids: Iterable[UUID]) -> dict[UUID, CommandRecord]:
        ids = list(command_ids)
        if not ids:
            return {}
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_COMMANDS_SQL, {"ids": ids})
            rows = await cur.fetchall()
        return {row[0]: CommandRecord(*row) for row in rows}

    async def hub_versions(self, hub_ids: Iterable[str]) -> dict[str, str | None]:
        ids = list(hub_ids)
        if not ids:
            return {}
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT hub_id, firmware_version FROM og.hub WHERE hub_id = ANY(%(ids)s)", {"ids": ids}
            )
            return {str(r[0]): r[1] for r in await cur.fetchall()}
