"""Postgres/process-backed implementations of `opengrid.guardian.ports` for the real `og-guardian`
process (`main.py`). Kept separate from `service.py`/`checks.py` so the decision logic has no
psycopg/subprocess import (BUILD.md S5a "pure logic separated from I/O"), mirroring `opengrid.trace.
pg_backend` and `opengrid.ledger.pg_backend`.

**Proposed-batch hand-off (02b S5.1 item 1: "engine -> guardian: proposed batch -- internal, not MQTT,
same process boundary is Postgres LISTEN/NOTIFY or a direct call").** MVP-S's schema (`og.command_batch`)
persists only batch-level metadata (`merkle_root`, `command_count`) -- the per-hub item content lives in
the batch's own K10 decision pre-image, which the engine must write to `og.trace` (`decision_type=
'RT_ALLOCATION'`, `event_class='RT_ALLOCATION'`) *before* invoking guardian, with a payload shaped like
`_trace_payload_to_proposal` below. `PgProposalPort` reads that same row back -- it is guardian's OWN
independent read of the pre-image, not a value the engine hands it a second time, so G-14 (pre-image
exists) and the proposal fetch are always consistent by construction.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from opengrid.core.physics import BankParams, HubParams
from opengrid.guardian.ports import (
    ActiveObligation,
    BankSnapshot,
    GuardianPorts,
    HubSnapshot,
    HubStatePort,
    L2Instruction,
    ProposedBatch,
    ProposedItem,
    SafeStopScope,
)
from opengrid.trace import TraceStore

logger = logging.getLogger(__name__)

_BANK_SNAPSHOT_SQL = "SELECT kva_rating, reserve_kva, feeder_id FROM og.bank WHERE bank_id = %(bank_id)s"

_BANK_LOAD_SQL = """
SELECT value FROM og.feed_obs
WHERE source = 'scada' AND product = %(bank_id)s AND series = 'APPARENT_POWER_KVA'
ORDER BY ts DESC LIMIT 1
"""

_ACTIVE_COMMITMENT_SQL = """
SELECT committed_kw FROM og.commitment
WHERE obligation_id = %(obligation_id)s AND supersedes IS NULL
ORDER BY interval_start DESC LIMIT 1
"""

# GUARD-01/K13: guardian's own, independent enumeration of every obligation with an ACTIVE commitment
# against this bank -- read-only from og.reservation/og.commitment, never from the proposed batch's own
# item list, so a batch cannot evade G-19 by omitting an obligation or relabelling it obligation_id=None.
_ACTIVE_OBLIGATIONS_FOR_BANK_SQL = """
SELECT DISTINCT c.obligation_id, c.committed_kw
FROM og.reservation r
JOIN og.commitment c ON c.obligation_id = r.obligation_id AND c.supersedes IS NULL
WHERE r.bank_id::text = %(bank_id)s AND r.released_at IS NULL
"""

_PRIOR_GRANT_SQL = """
SELECT granted_kw FROM og.grant WHERE obligation_id = %(obligation_id)s
ORDER BY created_at DESC LIMIT 1
"""

_STOP_STATE_SQL = """
SELECT action FROM og.stop_event WHERE scope_kind = %(scope_kind)s AND scope_ref = %(scope_ref)s
ORDER BY created_at DESC LIMIT 1
"""

_PROPOSAL_SQL = """
SELECT payload FROM og.trace
WHERE decision_type = 'RT_ALLOCATION' AND payload ->> 'command_batch_id' = %(command_batch_id)s
ORDER BY seq DESC LIMIT 1
"""


_ALL_HUB_PARAMS_SQL = "SELECT hub_id, e_kwh, r_kwh, p_kw, eta_c, eta_d FROM og.hub"


async def load_hub_params(pool: AsyncConnectionPool) -> dict[str, HubSnapshot]:
    """Seed `MqttHubStatePort` with every hub's static physical params (`og.hub`, config data -- not a
    live signal, so reading it from Postgres does not compromise the telemetry independence guardian
    otherwise keeps via MQTT). `soc_kwh`/`prev_p_kw` start at 0 and `health="stale"` until the first
    telemetry message for that hub arrives."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_ALL_HUB_PARAMS_SQL)
        rows = await cur.fetchall()
    return {
        hub_id: HubSnapshot(
            params=HubParams(e_kwh=e_kwh, r_kwh=r_kwh, p_kw=p_kw, eta_c=eta_c, eta_d=eta_d),
            soc_kwh=0.0,
            prev_p_kw=0.0,
            health="stale",
        )
        for hub_id, e_kwh, r_kwh, p_kw, eta_c, eta_d in rows
    }


class PgBankStatePort:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def snapshot(self, bank_id: str) -> BankSnapshot | None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_BANK_SNAPSHOT_SQL, {"bank_id": bank_id})
            bank_row = await cur.fetchone()
            if bank_row is None:
                return None
            await cur.execute(_BANK_LOAD_SQL, {"bank_id": bank_id})
            load_row = await cur.fetchone()
        kva_rating, reserve_kva, feeder_id = bank_row
        bank_load_kva = float(load_row[0]) if load_row else 0.0
        return BankSnapshot(
            params=BankParams(kva_rating=kva_rating, reserve_kva=reserve_kva),
            bank_load_kva=bank_load_kva,
            feeder_id=feeder_id,
            feeder_ceiling_kw_per_min=None,
        )


class LedgerModulePort:
    """Delegates to `opengrid.ledger.ledger_version()` (single owner, BUILD.md S1 "no duplicated
    functions") rather than re-reading `og.reservation` itself."""

    async def ledger_version(self) -> int:
        from opengrid import ledger

        return await ledger.ledger_version()


class PgCommitmentPort:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def active_kw(self, obligation_id: UUID, cycle_id: str) -> Decimal:
        _ = cycle_id  # MVP-S schema has no cycle-scoped commitment index; the latest active row applies
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_ACTIVE_COMMITMENT_SQL, {"obligation_id": obligation_id})
            row = await cur.fetchone()
        return Decimal(str(row[0])) if row else Decimal(0)

    async def active_obligations_for_bank(self, bank_id: str, cycle_id: str) -> list[ActiveObligation]:
        _ = cycle_id  # MVP-S schema has no cycle-scoped index; "released_at IS NULL" is the active set
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_ACTIVE_OBLIGATIONS_FOR_BANK_SQL, {"bank_id": bank_id})
            rows = await cur.fetchall()
        return [ActiveObligation(obligation_id=row[0], frozen_kw=Decimal(str(row[1]))) for row in rows]


class PgPriorGrantPort:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def prior_granted_kw(self, obligation_id: UUID) -> Decimal | None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_PRIOR_GRANT_SQL, {"obligation_id": obligation_id})
            row = await cur.fetchone()
        return Decimal(str(row[0])) if row else None


_LEASE_STATE_SELECT_SQL = "SELECT epoch, seq FROM og.lease_state WHERE bank_id = %(bank_id)s"

# Monotonic upsert (merge task, dispatch-live pass): never regresses a bank's recorded (epoch, seq) even
# under concurrent/out-of-order writers, so a delayed or replayed `record_accepted` call can never make
# G-13's freshness check MORE permissive than it already was.
_LEASE_STATE_UPSERT_SQL = """
INSERT INTO og.lease_state (bank_id, epoch, seq, updated_at)
VALUES (%(bank_id)s, %(epoch)s, %(seq)s, now())
ON CONFLICT (bank_id) DO UPDATE SET
    epoch = EXCLUDED.epoch, seq = EXCLUDED.seq, updated_at = now()
WHERE (og.lease_state.epoch, og.lease_state.seq) < (EXCLUDED.epoch, EXCLUDED.seq)
"""


class PgLeaseStatePort:
    """Durable per-bank (epoch, seq) high-water mark in `og.lease_state`
    (`migrations/0005_lease_state.sql`), so G-13 freshness survives a guardian restart instead of
    resetting every bank to (0, 0) (the prior `InMemoryLeaseStatePort`'s documented gap). A restart
    still reads back exactly what the last live guardian process last accepted, closing the "replay a
    batch right after a restart" window `InMemoryLeaseStatePort` left open (strictly more permissive of
    the first post-restart batch, not incorrect, but no longer necessary now the table exists)."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def last_accepted(self, bank_id: str) -> tuple[int, int]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_LEASE_STATE_SELECT_SQL, {"bank_id": bank_id})
            row = await cur.fetchone()
        return (int(row[0]), int(row[1])) if row else (0, 0)

    async def record_accepted(self, bank_id: str, epoch: int, seq: int) -> None:
        """Called only after a PASS verdict (`main.py`'s `tick()`), so a write failure here must never
        undo an already-finalized signing decision (K7) -- logged, not raised."""
        try:
            async with self._pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    _LEASE_STATE_UPSERT_SQL, {"bank_id": bank_id, "epoch": epoch, "seq": seq}
                )
                await conn.commit()
        except Exception:
            logger.exception(
                "failed to persist lease_state; next restart may re-permit this (epoch, seq)",
                extra={"bank_id": bank_id, "epoch": epoch, "seq": seq},
            )


class PgL2InstructionPort:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def active_instruction(self, bank_id: str) -> L2Instruction | None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT payload ->> 'kind', (payload ->> 'limit_kw')::float
                FROM og.trace
                WHERE decision_type = 'RT_ALLOCATION'
                  AND payload -> 'l2_instruction' ->> 'bank_id' = %(bank_id)s
                ORDER BY seq DESC LIMIT 1
                """,
                {"bank_id": bank_id},
            )
            row = await cur.fetchone()
        if row is None or row[0] is None:
            return None
        kind, limit_kw = row
        return L2Instruction(kind=kind, limit_kw=limit_kw)


class PgSafeStopPort:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def is_stopped(self, scope: SafeStopScope, scope_ref: str) -> bool:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_STOP_STATE_SQL, {"scope_kind": scope, "scope_ref": scope_ref})
            row = await cur.fetchone()
        if row is None:
            return False
        return bool(row[0] == "ENGAGE")


def _trace_payload_to_proposal(payload: dict[str, Any]) -> ProposedBatch:
    items = [
        ProposedItem(
            hub_id=i["hub_id"],
            p_kw_setpoint=float(i["p_kw_setpoint"]),
            reason_code=i["reason_code"],
            obligation_id=UUID(i["obligation_id"]) if i.get("obligation_id") else None,
            obligation_granted_kw=(
                Decimal(str(i["obligation_granted_kw"]))
                if i.get("obligation_granted_kw") is not None
                else None
            ),
        )
        for i in payload["items"]
    ]
    return ProposedBatch(
        command_batch_id=UUID(payload["command_batch_id"]),
        bank_id=payload["bank_id"],
        cycle_id=payload["cycle_id"],
        epoch=int(payload["epoch"]),
        seq=int(payload["seq"]),
        issued_at=datetime.fromisoformat(payload["issued_at"]),
        expires_at=datetime.fromisoformat(payload["expires_at"]),
        ledger_version=int(payload["ledger_version"]),
        items=items,
        is_firm_event=bool(payload.get("is_firm_event", False)),
    )


class PgProposalPort:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def fetch(self, command_batch_id: UUID) -> ProposedBatch | None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_PROPOSAL_SQL, {"command_batch_id": str(command_batch_id)})
            row = await cur.fetchone()
        if row is None:
            return None
        try:
            return _trace_payload_to_proposal(row[0])
        except (KeyError, ValueError, TypeError):
            logger.exception(
                "malformed RT_ALLOCATION trace payload", extra={"command_batch_id": str(command_batch_id)}
            )
            return None


class TraceStorePort:
    """Adapts `opengrid.trace.TraceStore` to the guardian's narrow `TracePort` (exists_preimage +
    append the verdict as its own trace event)."""

    def __init__(self, store: TraceStore, *, verdict_stream_id: str = "guardian") -> None:
        self._store = store
        self._stream_id = verdict_stream_id

    async def exists_preimage(self, decision_ref: UUID) -> bool:
        return await self._store.exists_preimage(decision_ref)

    async def append_verdict(self, batch_id: UUID, payload: dict[str, Any]) -> None:
        await self._store.append(
            self._stream_id,
            "GUARDIAN_VERDICT",
            "GUARDIAN_VERDICT",
            {**payload, "command_batch_id": str(batch_id)},
        )


class ChronyClockPort:
    """Reads the guardian host's NTP offset via `chronyc tracking` (K12/G-20). Falls back to 0.0 ms
    (never raises) if chrony is unavailable -- a monitoring gap is an alert, not a reason for the
    guardian process to crash; `og_guardian_clock_offset_ms` on `/metrics` still reflects the failure via
    a stale/zero reading, and `TS-06-18`'s injectable-offset property test exercises the real threshold
    logic against `checks.check_g20_clock_quality` directly, bypassing this adapter."""

    async def offset_from_ntp_ms(self) -> float:
        try:
            proc = await asyncio.create_subprocess_exec(
                "chronyc",
                "tracking",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=1.0)
        except (OSError, TimeoutError):
            return 0.0
        for raw_line in stdout.decode("ascii", errors="replace").splitlines():
            if raw_line.startswith("System time"):
                # "System time     : 0.000123456 seconds fast of NTP time"
                try:
                    seconds = float(raw_line.split(":")[1].split()[0])
                except (IndexError, ValueError):
                    return 0.0
                return seconds * 1000.0
        return 0.0


def build_pg_ports(
    pool: AsyncConnectionPool,
    trace_store: TraceStore,
    hubs: HubStatePort,
    *,
    zones_by_bank: dict[str, str] | None = None,
) -> tuple[GuardianPorts, PgLeaseStatePort]:
    """Convenience wiring for `main.py`: constructs every Postgres-backed port plus the durable lease
    tracker (returned separately so `main.py` can `await record_accepted(...)` after a PASS verdict).

    GUARD-02/04: `hubs` is a required argument, not a Postgres default -- guardian's hub-state read must
    always be its OWN telemetry (`opengrid.guardian.mqtt_io.MqttHubStatePort`), never `og.hub_state`
    (the row the engine/fleet processes maintain). There is deliberately no `PgHubStatePort` in this
    module for a caller to reach for by mistake.
    """
    leases = PgLeaseStatePort(pool)
    ports = GuardianPorts(
        clock=ChronyClockPort(),
        proposals=PgProposalPort(pool),
        trace=TraceStorePort(trace_store),
        hubs=hubs,
        banks=PgBankStatePort(pool),
        ledger=LedgerModulePort(),
        commitments=PgCommitmentPort(pool),
        prior_grants=PgPriorGrantPort(pool),
        leases=leases,
        l2_instructions=PgL2InstructionPort(pool),
        safe_stop=PgSafeStopPort(pool),
        zones_by_bank=dict(zones_by_bank or {}),
    )
    return ports, leases
