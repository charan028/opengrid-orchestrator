"""`og.delivery_record` reads and writes (migration 0050) and discovery of the calls to verify (D-38).

The read functions are the one delivery interface other modules use: the operator API, the customer API
(a utility's own calls), `opengrid.calls` status (measured delivered kW/kWh) and the grid link
(`fetch_live_points`). None of them re-derives delivery. Parameterised SQL only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from opengrid.core.manual_targets import SIGN_CONVENTION
from opengrid.core.services import canonical_product
from opengrid.delivery.models import CallKind, CallSpec, DeliveryRecord, LiveDeliveryPoint

#: The service type of a utility toll (D-29), when the call ledger has no kind for an older deployment.
UTILITY_SERVICE_TYPE = "REGULATED_CAPACITY"

_OPEN_DEPLOYMENTS_SQL = """
SELECT d.deployment_id, d.obligation_id, d.start_at, d.end_at, d.cancelled_at, d.requested_kw,
       o.committed_qty_kw, o.service_type, o.contract_id, c.customer_id, c.utility_id, c.variant,
       dc.call_id AS dispatch_call_id, dc.kind, dc.idempotency_key
FROM og.as_deployment d
JOIN og.obligation o ON o.obligation_id = d.obligation_id
JOIN og.contract c ON c.contract_id = o.contract_id
LEFT JOIN og.dispatch_call dc ON dc.deployment_id = d.deployment_id
LEFT JOIN og.delivery_record r ON r.call_id = d.deployment_id::text
WHERE d.start_at <= %(now)s AND d.end_at > %(since)s
  AND (d.cancelled_at IS NULL OR d.cancelled_at > %(since)s)
  AND (r.call_id IS NULL OR NOT r.final)
ORDER BY d.start_at
"""

_OPEN_MANUAL_SQL = """
SELECT t.trace_id, t.payload, t.created_at, r.final
FROM og.trace t
LEFT JOIN og.delivery_record r ON r.call_id = t.trace_id::text
WHERE t.event_class = 'MANUAL_TARGET' AND t.created_at > %(since)s AND t.created_at <= %(now)s
  AND (r.call_id IS NULL OR NOT r.final)
ORDER BY t.created_at
"""

_CANCELS_SQL = """
SELECT payload->>'cancels' AS cancels, min(created_at) AS at FROM og.trace
WHERE event_class = 'MANUAL_TARGET' AND created_at > %(since)s AND payload ? 'cancels'
GROUP BY 1
"""

_OBLIGATION_BANKS_SQL = """
SELECT bank_id FROM og.reservation
WHERE obligation_id = %(obligation_id)s AND interval_start < %(end)s AND interval_end > %(start)s
UNION
SELECT bank_id FROM og."grant"
WHERE obligation_id = %(obligation_id)s AND created_at >= %(start)s AND created_at < %(end)s
"""

_HUB_BANKS_SQL = "SELECT DISTINCT bank_id FROM og.hub WHERE hub_id = ANY(%(hubs)s)"

_METER_BANKS_SQL = """
SELECT DISTINCT bank_id FROM og.asset
WHERE asset_class = 'SUBSTATION' AND status = 'ACTIVE' AND bank_id = ANY(%(banks)s)
"""

_COLUMNS = (
    "call_id, call_kind, deployment_id, dispatch_call_id, obligation_id, contract_id, customer_id, utility_id, "
    "service_type, product, bank_ids, meter_bank_ids, hub_ids, window_start, window_end, stopped_at, "
    "committed_kw, commanded_kw_avg, delivered_kw_avg, delivered_kw_last, commanded_kw_last, ramp_time_s, "
    "time_to_target_s, sustained_pct, lowest_kw, lowest_at, lowest_run_s, discharged_kwh, committed_kwh, "
    "stale_frac, result, reasons, meter_status, meter_mismatch_frac, meter_baseline_kw, battery_baseline_kw, "
    "series, evaluated_to, final, trace_id, series_pruned_at"
)
_UPDATABLE = [c.strip() for c in _COLUMNS.split(",") if c.strip() != "call_id"]

_UPSERT_SQL = (
    f"INSERT INTO og.delivery_record ({_COLUMNS}) VALUES ("  # noqa: S608 -- fixed column list
    + ", ".join(f"%({c.strip()})s" for c in _COLUMNS.split(","))
    + ") ON CONFLICT (call_id) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in _UPDATABLE)
    + ", updated_at = now()"
)

_SELECT = f"SELECT {_COLUMNS}, updated_at FROM og.delivery_record"  # noqa: S608 -- fixed column list
_SUMMARY_COLUMNS = _COLUMNS.replace("series, ", "")
_SELECT_SUMMARY = f"SELECT {_SUMMARY_COLUMNS}, updated_at FROM og.delivery_record"  # noqa: S608

#: Obligations whose latest AT_RISK event is one this job set (cause measured_delivery) and still flagged:
#: what a restarted og-settle must reconcile (its in-memory set is empty).
_DELIVERY_AT_RISK_SQL = """
WITH latest AS (
    SELECT DISTINCT ON (t.stream_id) t.stream_id, t.event_class, t.payload
    FROM og.trace t
    WHERE t.event_class IN ('AT_RISK', 'AT_RISK_CLEARED') AND t.stream_id LIKE 'obligation-%%'
      AND t.created_at > %(since)s
    ORDER BY t.stream_id, t.created_at DESC
)
SELECT o.obligation_id, latest.payload->>'call_id' AS call_id
FROM latest JOIN og.obligation o ON latest.stream_id = 'obligation-' || o.obligation_id::text
WHERE latest.event_class = 'AT_RISK' AND latest.payload->>'cause' = 'measured_delivery' AND o.at_risk
"""

#: The newest final meter check per metered bank since `since` (to auto-clear a meter mismatch once the
#: same meter agrees with battery telemetry again on a later call).
_LATEST_METER_SQL = """
SELECT DISTINCT ON (bank) bank, meter_status, window_end
FROM og.delivery_record, unnest(meter_bank_ids) AS bank
WHERE final AND meter_bank_ids && %(banks)s::text[] AND window_end > %(since)s
  AND meter_status IN ('CORROBORATED', 'UNCORROBORATED')
ORDER BY bank, window_end DESC
"""

#: Retention (migration 0051): empty the per-bucket series of final records older than the keep horizon, a
#: bounded batch per pass; the summary columns are kept like og.as_deployment.
_PRUNE_SERIES_SQL = """
UPDATE og.delivery_record SET series = '[]'::jsonb, series_pruned_at = %(now)s
WHERE call_id IN (
    SELECT call_id FROM og.delivery_record
    WHERE final AND series_pruned_at IS NULL AND window_end < %(cutoff)s
    ORDER BY window_end
    LIMIT %(batch)s
)
"""

_SUMMARY_SQL = """
SELECT contract_id, service_type, (window_start AT TIME ZONE 'America/Chicago')::date AS day,
       count(*) AS calls,
       count(*) FILTER (WHERE result = 'PASS') AS passed,
       count(*) FILTER (WHERE result = 'PARTIAL') AS partial,
       count(*) FILTER (WHERE result = 'FAIL') AS failed,
       count(*) FILTER (WHERE meter_status = 'UNCORROBORATED') AS uncorroborated,
       avg(sustained_pct)::float8 AS sustained_pct_avg,
       sum(discharged_kwh)::float8 AS discharged_kwh,
       sum(committed_kwh)::float8 AS committed_kwh
FROM og.delivery_record
WHERE final AND window_start >= %(since)s AND window_start < %(until)s
GROUP BY 1, 2, 3
ORDER BY 3 DESC, 1
"""


def _f(value: Any) -> float | None:
    return float(value) if value is not None else None


def spec_from_deployment(row: Mapping[str, Any]) -> CallSpec:
    """A deployment row (`_OPEN_DEPLOYMENTS_SQL`) as the call to verify. Committed kW: the call's own
    requested kW (signed), else the obligation's full committed kW as discharge."""
    requested = _f(row["requested_kw"])
    committed = requested if requested is not None else -(_f(row["committed_qty_kw"]) or 0.0)
    kind = (
        CallKind.UTILITY_CALL
        if row.get("kind") == "UTILITY_CALL" or row["service_type"] == UTILITY_SERVICE_TYPE
        else CallKind.AS_DEPLOYMENT
    )
    stopped = (
        row["cancelled_at"]
        if row["cancelled_at"] is not None and row["cancelled_at"] < row["end_at"]
        else None
    )
    return CallSpec(
        call_id=str(row["deployment_id"]),
        call_kind=kind,
        window_start=row["start_at"],
        window_end=row["end_at"],
        committed_kw=committed,
        product=canonical_product(row["variant"]),
        deployment_id=row["deployment_id"],
        dispatch_call_id=row.get("dispatch_call_id"),
        obligation_id=row["obligation_id"],
        contract_id=row["contract_id"],
        customer_id=row["customer_id"],
        utility_id=row["utility_id"],
        service_type=row["service_type"],
        stopped_at=stopped,
        idempotency_key=row.get("idempotency_key"),
    )


def spec_from_manual(
    trace_id: Any, payload: Mapping[str, Any], created_at: datetime, cancelled_at: datetime | None
) -> CallSpec | None:
    """A MANUAL_TARGET discharge row (`p_kw_command` < 0, per hub) as a call; None for a charge target,
    a cancel row or a malformed/foreign-convention row (the one parser's rules, `core.manual_targets`)."""
    if payload.get("cancels") or payload.get("sign_convention", SIGN_CONVENTION) != SIGN_CONVENTION:
        return None
    try:
        raw = payload["p_kw_command"] if "p_kw_command" in payload else payload["p_kw_target"]
        per_hub_kw = float(raw)
        hubs = tuple(str(h) for h in payload["hub_ids"])
        start = datetime.fromisoformat(str(payload.get("issued_at") or created_at.isoformat()))
        end = datetime.fromisoformat(str(payload["expires_at"]))
    except (KeyError, TypeError, ValueError):
        return None
    if per_hub_kw >= 0.0 or not hubs or end <= start:
        return None
    return CallSpec(
        call_id=str(trace_id),
        call_kind=CallKind.MANUAL_TARGET,
        window_start=start,
        window_end=end,
        committed_kw=per_hub_kw * len(hubs),
        product="MANUAL",
        stopped_at=cancelled_at if cancelled_at is not None and cancelled_at < end else None,
        hub_ids=hubs,
    )


async def _all(conn: AsyncConnection[Any], sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, dict(params))
        return list(await cur.fetchall())


async def open_calls(conn: AsyncConnection[Any], *, now: datetime, lookback_s: float) -> list[CallSpec]:
    """Calls started by `now` that ended within `lookback_s` and have no final record yet."""
    since = now - timedelta(seconds=lookback_s)
    specs = [
        spec_from_deployment(r) for r in await _all(conn, _OPEN_DEPLOYMENTS_SQL, {"now": now, "since": since})
    ]
    cancels = {
        str(r["cancels"]): r["at"]
        for r in await _all(conn, _CANCELS_SQL, {"since": since - timedelta(hours=24)})
    }
    for row in await _all(conn, _OPEN_MANUAL_SQL, {"now": now, "since": since - timedelta(hours=24)}):
        spec = spec_from_manual(
            row["trace_id"], row["payload"], row["created_at"], cancels.get(str(row["trace_id"]))
        )
        if spec is not None and spec.window_start <= now and spec.effective_end > since:
            specs.append(spec)
    return specs


async def resolve_banks(
    conn: AsyncConnection[Any], spec: CallSpec, *, extra_meter_banks: Sequence[str] = ()
) -> CallSpec:
    """The call's banks (an obligation's reserved or granted banks; a manual target's hubs' banks) and
    which of them have an independent meter (a SUBSTATION asset's bank, or `extra_meter_banks`)."""
    if spec.obligation_id is not None:
        rows = await _all(
            conn,
            _OBLIGATION_BANKS_SQL,
            {"obligation_id": spec.obligation_id, "start": spec.window_start, "end": spec.effective_end},
        )
    else:
        rows = await _all(conn, _HUB_BANKS_SQL, {"hubs": list(spec.hub_ids)})
    banks = tuple(sorted({str(r["bank_id"]) for r in rows}))
    metered = {str(r["bank_id"]) for r in await _all(conn, _METER_BANKS_SQL, {"banks": list(banks)})}
    metered |= {b for b in extra_meter_banks if b in banks}
    if spec.obligation_id is None:
        metered = set()  # a manual target's hubs are part of a bank; the bank meter also sees the others
    return replace(spec, bank_ids=banks, meter_bank_ids=tuple(sorted(metered)))


def _record_params(record: DeliveryRecord) -> dict[str, Any]:
    body = record.model_dump(exclude={"updated_at"})
    body["call_kind"] = record.call_kind.value
    body["series"] = Jsonb([p.model_dump(mode="json") for p in record.series])
    return body


async def upsert_record(conn: AsyncConnection[Any], record: DeliveryRecord) -> None:
    async with conn.cursor() as cur:
        await cur.execute(_UPSERT_SQL, _record_params(record))


def _record(row: Mapping[str, Any]) -> DeliveryRecord:
    return DeliveryRecord.model_validate({**row, "series": row.get("series") or []})


async def fetch_record(pool: AsyncConnectionPool, call_id: str) -> DeliveryRecord | None:
    """The delivery record of one call (`og.as_deployment.deployment_id`, or a MANUAL_TARGET trace id)."""
    async with pool.connection() as conn:
        rows = await _all(conn, _SELECT + " WHERE call_id = %(call_id)s", {"call_id": str(call_id)})
    return _record(rows[0]) if rows else None


async def fetch_open_records(
    conn: AsyncConnection[Any], call_ids: Sequence[str]
) -> dict[str, DeliveryRecord]:
    rows = await _all(conn, _SELECT + " WHERE call_id = ANY(%(ids)s)", {"ids": list(call_ids)})
    return {r["call_id"]: _record(r) for r in rows}


async def list_records(
    pool: AsyncConnectionPool,
    *,
    customer_id: UUID | None = None,
    utility_id: str | None = None,
    contract_id: UUID | None = None,
    service_type: str | None = None,
    call_kind: str | None = None,
    result: str | None = None,
    call_ids: Sequence[str] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 200,
) -> list[DeliveryRecord]:
    """Records newest first (without their series), filtered on any combination of the arguments."""
    sql = (
        _SELECT_SUMMARY
        + " WHERE (%(customer_id)s::uuid IS NULL OR customer_id = %(customer_id)s)"
        + " AND (%(utility_id)s::text IS NULL OR utility_id = %(utility_id)s)"
        + " AND (%(contract_id)s::uuid IS NULL OR contract_id = %(contract_id)s)"
        + " AND (%(service_type)s::text IS NULL OR service_type = %(service_type)s)"
        + " AND (%(call_kind)s::text IS NULL OR call_kind = %(call_kind)s)"
        + " AND (%(result)s::text IS NULL OR result = %(result)s)"
        + " AND (%(call_ids)s::text[] IS NULL OR call_id = ANY(%(call_ids)s))"
        + " AND (%(since)s::timestamptz IS NULL OR window_start >= %(since)s)"
        + " AND (%(until)s::timestamptz IS NULL OR window_start < %(until)s)"
        + " ORDER BY window_start DESC LIMIT %(limit)s"
    )
    params = {
        "customer_id": customer_id,
        "utility_id": utility_id,
        "contract_id": contract_id,
        "service_type": service_type,
        "call_kind": call_kind,
        "result": result,
        "call_ids": list(call_ids) if call_ids is not None else None,
        "since": since,
        "until": until,
        "limit": limit,
    }
    async with pool.connection() as conn:
        return [_record(r) for r in await _all(conn, sql, params)]


async def summary(pool: AsyncConnectionPool, *, since: datetime, until: datetime) -> list[dict[str, Any]]:
    """Per contract and CT day: calls, results, meter mismatches, mean sustained compliance, energy."""
    async with pool.connection() as conn:
        rows = await _all(conn, _SUMMARY_SQL, {"since": since, "until": until})
    return [
        {
            **{k: v for k, v in r.items() if k not in ("contract_id", "day")},
            "contract_id": str(r["contract_id"]) if r["contract_id"] else None,
            "day": r["day"].isoformat(),
            "compliance_pct": 100.0 * r["passed"] / r["calls"] if r["calls"] else None,
        }
        for r in rows
    ]


async def prune_series(conn: AsyncConnection[Any], *, now: datetime, keep_days: float, batch: int) -> int:
    """Empty the series of up to `batch` final records whose window ended over `keep_days` ago; returns how
    many. The summary (result, energy, meter check) stays."""
    async with conn.cursor() as cur:
        await cur.execute(
            _PRUNE_SERIES_SQL, {"now": now, "cutoff": now - timedelta(days=keep_days), "batch": batch}
        )
        return int(cur.rowcount or 0)


async def delivery_at_risk_flags(
    conn: AsyncConnection[Any], *, since: datetime
) -> list[tuple[UUID, str | None]]:
    """(obligation_id, call_id) still AT_RISK from a measured-delivery flag this job set (restart reconcile)."""
    rows = await _all(conn, _DELIVERY_AT_RISK_SQL, {"since": since})
    return [(r["obligation_id"], r["call_id"]) for r in rows]


async def latest_meter_status(
    conn: AsyncConnection[Any], banks: Sequence[str], *, since: datetime
) -> dict[str, tuple[str, datetime]]:
    """Per metered bank, the newest final meter check (status, call window end) since `since`."""
    rows = await _all(conn, _LATEST_METER_SQL, {"banks": list(banks), "since": since})
    return {str(r["bank"]): (str(r["meter_status"]), r["window_end"]) for r in rows}


def live_point(
    record: DeliveryRecord, *, now: datetime, idempotency_key: str | None = None
) -> LiveDeliveryPoint:
    last = record.series[-1] if record.series else None
    end = min(record.window_end, record.stopped_at) if record.stopped_at else record.window_end
    return LiveDeliveryPoint(
        call_id=record.call_id,
        call_kind=record.call_kind,
        deployment_id=record.deployment_id,
        dispatch_call_id=record.dispatch_call_id,
        idempotency_key=idempotency_key,
        obligation_id=record.obligation_id,
        contract_id=record.contract_id,
        utility_id=record.utility_id,
        bank_ids=record.bank_ids,
        committed_kw=record.committed_kw,
        commanded_kw=record.commanded_kw_last,
        delivered_kw=record.delivered_kw_last,
        discharged_kwh=record.discharged_kwh,
        state="ACTIVE" if now < end else "ENDED",
        result=record.result,
        meter_status=record.meter_status,
        stale=last is None or last.d is None,
        as_of=record.evaluated_to,
    )


_LIVE_SQL = (
    _SELECT_SUMMARY.replace(" FROM og.delivery_record", ", NULL::jsonb AS series FROM og.delivery_record")
    + " WHERE window_start <= %(now)s AND window_end > %(since)s"
    + " AND (%(utility_id)s::text IS NULL OR utility_id = %(utility_id)s)"
    + " ORDER BY window_start"
)

_LAST_POINT_SQL = """
SELECT r.call_id, r.series -> -1 AS last, dc.idempotency_key
FROM og.delivery_record r LEFT JOIN og.dispatch_call dc ON dc.call_id = r.dispatch_call_id
WHERE r.call_id = ANY(%(ids)s)
"""


async def fetch_live_points(
    pool: AsyncConnectionPool,
    *,
    utility_id: str | None = None,
    now: datetime | None = None,
    ended_within_s: float = 300.0,
) -> list[LiveDeliveryPoint]:
    """One point per call running now or ended within `ended_within_s` (the grid link's
    CALL_DELIVERED_KW / call state). Signed kW; `stale` when the latest bucket had no telemetry."""
    now = now or datetime.now(UTC)
    params = {"now": now, "since": now - timedelta(seconds=ended_within_s), "utility_id": utility_id}
    async with pool.connection() as conn:
        rows = await _all(conn, _LIVE_SQL, params)
        ids = [r["call_id"] for r in rows]
        extra = {r["call_id"]: r for r in await _all(conn, _LAST_POINT_SQL, {"ids": ids})} if ids else {}
    points = []
    for row in rows:
        last = (extra.get(row["call_id"]) or {}).get("last")
        record = _record({**row, "series": [last] if last else []})
        points.append(
            live_point(
                record, now=now, idempotency_key=(extra.get(row["call_id"]) or {}).get("idempotency_key")
            )
        )
    return points
