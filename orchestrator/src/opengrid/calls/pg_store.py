"""`PgCallStore`: `opengrid.calls.ports.CallStore` over Postgres -- the call ledger `og.dispatch_call`
and `og.as_deployment` (migrations 0020, 0047), with the obligation reads the checks need. Parameterised
SQL only. The accepted-call write is one transaction under a per-obligation advisory lock, so the overlap
check and the insert cannot interleave with a concurrent call on the same obligation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg import errors
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from opengrid.calls.models import AwardView, CallKind, CallRecord, Delivered
from opengrid.calls.ports import IdempotencyKeyTakenError, OverlapError
from opengrid.calls.rules import DEPLOYABLE_STATES, TOLLING_SERVICE_TYPE, TOLLING_VARIANT
from opengrid.health.model import AlertFinding, AlertSeverity
from opengrid.health.queries import raise_alert as write_alert

#: Allocator cycles are ~2 s apart; a longer gap (engine restart) is not counted as delivery.
MAX_CYCLE_GAP_S = 10.0

_AWARD_SQL = """
SELECT o.obligation_id, o.service_type, c.variant, o.state, pr.duration_minutes, o.committed_qty_kw,
       o.window_start, o.window_end, c.utility_id
FROM og.obligation o
JOIN og.contract c ON c.contract_id = o.contract_id
LEFT JOIN og.opportunity op ON op.opportunity_id = o.opportunity_id
LEFT JOIN og.product_rule pr ON pr.product_rule_id = op.product_rule_id
WHERE o.obligation_id = %(obligation_id)s
"""

_TOLL_OBLIGATION_SQL = """
SELECT o.obligation_id
FROM og.obligation o
JOIN og.contract c ON c.contract_id = o.contract_id
WHERE c.utility_id = %(utility_id)s
  AND o.service_type = %(service_type)s AND upper(coalesce(c.variant, '')) = %(variant)s
  AND o.state = ANY(%(states)s)
  AND o.window_start <= %(at)s AND o.window_end > %(at)s
ORDER BY o.window_start
LIMIT 1
"""

_TOLL_OBLIGATIONS_SQL = """
SELECT o.obligation_id, o.service_type, c.variant, o.state, pr.duration_minutes, o.committed_qty_kw,
       o.window_start, o.window_end, c.utility_id
FROM og.obligation o
JOIN og.contract c ON c.contract_id = o.contract_id
LEFT JOIN og.opportunity op ON op.opportunity_id = o.opportunity_id
LEFT JOIN og.product_rule pr ON pr.product_rule_id = op.product_rule_id
WHERE c.utility_id = %(utility_id)s
  AND o.service_type = %(service_type)s AND upper(coalesce(c.variant, '')) = %(variant)s
  AND o.window_start >= %(since)s AND o.window_start < %(until)s
ORDER BY o.window_start
"""

_RECORD_SELECT = """
SELECT c.call_id, c.outcome, c.origin, c.principal, c.reason, c.start_at,
       coalesce(d.end_at, c.end_at) AS end_at, c.duration_minutes, c.reason_code, c.detail,
       c.deployment_id, c.obligation_id, c.utility_id, c.kind, c.requested_kw, c.committed_kw,
       c.idempotency_key, c.trace_id, c.created_at, d.cancelled_at, c.request_hash
FROM og.dispatch_call c
LEFT JOIN og.as_deployment d ON d.deployment_id = c.deployment_id
"""

_OVERLAP_SQL = """
SELECT 1 FROM og.as_deployment d
WHERE d.cancelled_at IS NULL AND d.start_at < %(end_at)s AND d.end_at > %(start_at)s
  AND (d.obligation_id = %(obligation_id)s OR (d.obligation_id IS NULL AND %(is_as)s))
LIMIT 1
"""

_INSERT_DEPLOYMENT_SQL = """
INSERT INTO og.as_deployment
    (deployment_id, obligation_id, start_at, end_at, source, requested_by, reason, requested_kw, call_id)
VALUES (%(deployment_id)s, %(obligation_id)s, %(start_at)s, %(end_at)s, %(source)s, %(principal)s,
        %(reason)s, %(requested_kw)s, %(call_id)s)
"""

_INSERT_CALL_SQL = """
INSERT INTO og.dispatch_call
    (call_id, origin, principal, idempotency_key, request_hash, outcome, reason_code, detail, obligation_id,
     utility_id, kind, requested_kw, committed_kw, start_at, end_at, duration_minutes, reason, deployment_id,
     trace_id)
VALUES (%(call_id)s, %(origin)s, %(principal)s, %(idempotency_key)s, %(request_hash)s, %(outcome)s,
        %(reason_code)s, %(detail)s, %(obligation_id)s, %(utility_id)s, %(kind)s, %(requested_kw)s,
        %(committed_kw)s, %(start_at)s, %(end_at)s, %(duration_minutes)s, %(reason)s, %(deployment_id)s,
        %(trace_id)s)
"""

_INSERT_OPERATOR_ACTION_SQL = """
INSERT INTO og.operator_action
    (operator_action_id, operator_ref, action_kind, target_ref, tier, reason, confirmed_at, approver_ref,
     trace_id)
VALUES (%(id)s, %(principal)s, 'MANUAL_COMMAND', %(target_ref)s, 'TIER1', %(reason)s, now(), %(principal)s,
        %(trace_id)s)
"""

_END_DEPLOYMENT_SQL = """
UPDATE og.as_deployment
SET cancelled_at = CASE WHEN %(end_at)s <= %(now)s OR %(end_at)s <= start_at THEN %(now)s ELSE NULL END,
    end_at = CASE WHEN %(end_at)s <= %(now)s OR %(end_at)s <= start_at THEN end_at ELSE %(end_at)s END
WHERE deployment_id = %(deployment_id)s AND cancelled_at IS NULL AND end_at > %(now)s
"""

# Per allocator cycle: the obligation's granted discharge (kW, summed over its banks), integrated over
# time to the next cycle (capped at MAX_CYCLE_GAP_S) for the kWh. The latest cycle is reported only if
# it is recent (within the gap of the window's end): a stalled engine reports no current kW.
_DELIVERED_SQL = """
WITH cyc AS (
    SELECT g.cycle_id, min(g.created_at) AS t, sum(g.granted_kw) AS kw
    FROM og."grant" g
    WHERE g.obligation_id = %(obligation_id)s AND NOT g.is_headroom
      AND g.created_at >= %(start_at)s AND g.created_at < %(end_at)s
    GROUP BY g.cycle_id
), seq AS (
    SELECT t, kw, lead(t) OVER (ORDER BY t) AS next_t FROM cyc
)
SELECT
    (SELECT kw FROM seq WHERE t >= %(end_at)s - make_interval(secs => %(gap_s)s) ORDER BY t DESC LIMIT 1)
        AS last_kw,
    coalesce(sum(greatest(kw, 0) * least(extract(epoch FROM (coalesce(next_t, %(end_at)s) - t)), %(gap_s)s)
                 / 3600.0), 0) AS kwh
FROM seq
"""

_OPEN_ALERT_SQL = """
SELECT 1 FROM og.alert
WHERE rule = %(rule)s AND scope_ref IS NOT DISTINCT FROM %(scope_ref)s AND cleared_at IS NULL AND acked_by IS NULL
LIMIT 1
"""


def _f(value: Any) -> float | None:
    return float(value) if value is not None else None


def _award_from(row: dict[str, Any]) -> AwardView:
    return AwardView(
        obligation_id=row["obligation_id"],
        service_type=row["service_type"],
        variant=row["variant"],
        state=row["state"],
        duration_minutes=row["duration_minutes"],
        committed_kw=_f(row["committed_qty_kw"]),
        window_start=row["window_start"],
        window_end=row["window_end"],
        utility_id=row["utility_id"],
    )


def _record_from(row: dict[str, Any]) -> CallRecord:
    return CallRecord.model_validate({k: v for k, v in row.items() if k != "request_hash"})


def _call_params(record: CallRecord, fingerprint: str) -> dict[str, Any]:
    return {
        **record.model_dump(exclude={"replayed", "cancelled_at", "created_at"}),
        "origin": record.origin.value,
        "outcome": record.outcome.value,
        "kind": record.kind.value if record.kind else None,
        "request_hash": fingerprint,
    }


class PgCallStore:
    """`CallStore` on a psycopg 3 async pool (the caller's; any process may construct one)."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def _one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchone()

    async def get_award(self, obligation_id: UUID) -> AwardView | None:
        row = await self._one(_AWARD_SQL, {"obligation_id": obligation_id})
        return _award_from(row) if row is not None else None

    async def toll_obligations(self, utility_id: str, since: datetime, until: datetime) -> list[AwardView]:
        params = {
            "utility_id": utility_id,
            "service_type": TOLLING_SERVICE_TYPE,
            "variant": TOLLING_VARIANT,
            "since": since,
            "until": until,
        }
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_TOLL_OBLIGATIONS_SQL, params)
            return [_award_from(row) for row in await cur.fetchall()]

    async def find_toll_obligation(self, utility_id: str, at: datetime) -> UUID | None:
        row = await self._one(
            _TOLL_OBLIGATION_SQL,
            {
                "utility_id": utility_id,
                "service_type": TOLLING_SERVICE_TYPE,
                "variant": TOLLING_VARIANT,
                "states": sorted(DEPLOYABLE_STATES),
                "at": at,
            },
        )
        return row["obligation_id"] if row is not None else None

    async def find_by_key(self, principal: str, idempotency_key: str) -> tuple[CallRecord, str] | None:
        row = await self._one(
            _RECORD_SELECT + " WHERE c.principal = %(principal)s AND c.idempotency_key = %(key)s",
            {"principal": principal, "key": idempotency_key},
        )
        return (_record_from(row), str(row["request_hash"])) if row is not None else None

    async def count_calls_since(self, principal: str, since: datetime) -> int:
        row = await self._one(
            "SELECT count(*) AS n FROM og.dispatch_call WHERE principal = %(principal)s AND created_at >= %(since)s",
            {"principal": principal, "since": since},
        )
        return int(row["n"]) if row is not None else 0

    async def insert_accepted(self, record: CallRecord, *, fingerprint: str, source: str) -> CallRecord:
        deployment_id = uuid4()
        stored = record.model_copy(update={"deployment_id": deployment_id, "created_at": datetime.now(UTC)})
        params = _call_params(stored, fingerprint)
        target = "UTILITY_CALL" if record.kind is CallKind.UTILITY_CALL else "AS_DEPLOYMENT"
        try:
            async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%(key)s, 0))",
                    {"key": f"dispatch_call:{record.obligation_id}"},
                )
                await cur.execute(
                    _OVERLAP_SQL,
                    {
                        "obligation_id": record.obligation_id,
                        "start_at": record.start_at,
                        "end_at": record.end_at,
                        "is_as": record.kind is CallKind.AS,
                    },
                )
                if await cur.fetchone() is not None:
                    raise OverlapError(str(record.obligation_id))
                await cur.execute(_INSERT_DEPLOYMENT_SQL, {**params, "source": source})
                await cur.execute(_INSERT_CALL_SQL, params)
                await cur.execute(
                    _INSERT_OPERATOR_ACTION_SQL,
                    {
                        "id": uuid4(),
                        "principal": record.principal,
                        "target_ref": f"{target}:{record.obligation_id}",
                        "reason": record.reason,
                        "trace_id": record.trace_id,
                    },
                )
        except errors.UniqueViolation as exc:
            raise IdempotencyKeyTakenError(record.principal) from exc
        return stored

    async def insert_refused(self, record: CallRecord, *, fingerprint: str) -> CallRecord:
        stored = record.model_copy(update={"created_at": datetime.now(UTC)})
        try:
            async with self._pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(_INSERT_CALL_SQL, _call_params(stored, fingerprint))
        except errors.UniqueViolation as exc:
            raise IdempotencyKeyTakenError(record.principal) from exc
        return stored

    async def get_call(self, call_id: UUID) -> CallRecord | None:
        row = await self._one(_RECORD_SELECT + " WHERE c.call_id = %(call_id)s", {"call_id": call_id})
        return _record_from(row) if row is not None else None

    async def get_call_by_deployment(self, deployment_id: UUID) -> CallRecord | None:
        row = await self._one(
            _RECORD_SELECT + " WHERE c.deployment_id = %(deployment_id)s", {"deployment_id": deployment_id}
        )
        return _record_from(row) if row is not None else None

    async def list_calls(
        self,
        *,
        utility_id: str | None = None,
        principal: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[CallRecord]:
        sql = (
            _RECORD_SELECT
            + " WHERE (%(utility_id)s::text IS NULL OR c.utility_id = %(utility_id)s)"
            + " AND (%(principal)s::text IS NULL OR c.principal = %(principal)s)"
            + " AND (%(since)s::timestamptz IS NULL OR c.created_at >= %(since)s)"
            + " ORDER BY c.created_at DESC LIMIT %(limit)s"
        )
        params = {"utility_id": utility_id, "principal": principal, "since": since, "limit": limit}
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return [_record_from(row) for row in await cur.fetchall()]

    async def end_deployment(self, deployment_id: UUID, *, end_at: datetime, now: datetime) -> bool:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _END_DEPLOYMENT_SQL, {"deployment_id": deployment_id, "end_at": end_at, "now": now}
            )
            return cur.rowcount > 0

    async def delivered(self, obligation_id: UUID, start: datetime, end: datetime) -> Delivered:
        row = await self._one(
            _DELIVERED_SQL,
            {"obligation_id": obligation_id, "start_at": start, "end_at": end, "gap_s": MAX_CYCLE_GAP_S},
        )
        if row is None:
            return Delivered(last_kw=None, kwh=0.0)
        return Delivered(last_kw=_f(row["last_kw"]), kwh=float(row["kwh"] or 0.0))

    async def raise_alert(
        self, rule: str, severity: AlertSeverity, summary: str, detail: dict[str, Any]
    ) -> None:
        if await self._one(_OPEN_ALERT_SQL, {"rule": rule, "scope_ref": detail.get("scope_ref")}) is not None:
            return
        finding = AlertFinding(
            rule=rule,
            severity=severity,
            summary=summary,
            condition_key=f"{rule}:{detail.get('scope_ref')}",
            detail=dict(detail),
        )
        await write_alert(self._pool, finding, opened_at=datetime.now(UTC))
