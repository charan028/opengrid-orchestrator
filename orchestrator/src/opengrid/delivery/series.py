"""Measured series of a call, aligned to its window (D-38): per bucket the committed, commanded (signed
batches) and delivered kW, command cycles proposed/vetoed, and the independent meter against battery
telemetry. The SQL reads one slice `[a, b)` at a time (the job appends slices as a call runs), so a
90-minute call on 50 home banks is never re-read. `assemble_buckets` is the pure part (unit-tested).

- delivered, obligation calls (utility toll, ERCOT AS): per bank, the hubs' measured discharge attributed
  by the obligation's share of the bank's granted kW (`opengrid.core.delivery.attributed_discharge_kw`,
  the rule settlement meters by), summed over the obligation's reserved banks;
- delivered, manual targets: the target hubs' own measured discharge;
- commanded: the call's items in `RT_ALLOCATION` batches the guardian signed (verdict PASS); an unsigned
  (vetoed, timed-out) batch commands 0 kW. Averaged per bank over the bucket's cycles, summed over banks;
- meter: SCADA `REAL_POWER_KW` (GOOD quality) on the metered banks minus its pre-call baseline, against
  the same banks' battery telemetry minus its baseline (feeder background load cancels).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from psycopg import AsyncConnection

from opengrid.core.delivery import DeliveryBucket, attributed_discharge_kw
from opengrid.core.reasons import R_MANUAL_RAMP
from opengrid.delivery.models import CallSpec

#: Outcomes that leave a bank batch unsigned because the guardian vetoed it (it signs only PASS).
VETO_OUTCOMES = frozenset({"VETOED", "PARTLY_VETOED"})

_BIN = "date_bin(make_interval(secs => %(bucket_s)s), {col}, %(origin)s)"

_SHARES_SQL = f"""
WITH ob AS (
    SELECT DISTINCT cycle_id FROM og."grant"
    WHERE obligation_id = %(obligation_id)s AND created_at >= %(a)s AND created_at < %(b)s
), cyc AS (
    SELECT {_BIN.format(col="min(g.created_at)")} AS b, g.cycle_id, g.bank_id,
           coalesce(sum(g.granted_kw) FILTER (WHERE g.obligation_id = %(obligation_id)s), 0) AS ob_kw,
           sum(g.granted_kw) AS tot_kw
    FROM og."grant" g
    WHERE g.cycle_id IN (SELECT cycle_id FROM ob) AND g.bank_id = ANY(%(banks)s)
    GROUP BY g.cycle_id, g.bank_id
)
SELECT b, bank_id, avg(ob_kw)::float8 AS ob_kw, avg(tot_kw)::float8 AS tot_kw FROM cyc GROUP BY 1, 2
"""  # noqa: S608 -- _BIN is a fixed module literal

_TELEMETRY_SQL = f"""
SELECT {_BIN.format(col="t.ts")} AS b, h.bank_id, t.hub_id, avg(t.p_kw)::float8 AS p
FROM og.telemetry t JOIN og.hub h USING (hub_id)
WHERE t.ts >= %(a)s AND t.ts < %(b)s
  AND ((%(by_hub)s AND t.hub_id = ANY(%(hubs)s)) OR (NOT %(by_hub)s AND h.bank_id = ANY(%(banks)s)))
GROUP BY 1, 2, 3
"""  # noqa: S608 -- _BIN is a fixed module literal

_COMMANDED_SQL = f"""
SELECT {_BIN.format(col="t.created_at")} AS b, t.stream_id, t.payload->>'command_batch_id' AS batch,
       v.outcome,
       coalesce(sum((i->>'p_kw_setpoint')::float8) FILTER (
           WHERE (%(obligation)s::text IS NOT NULL AND i->>'obligation_id' = %(obligation)s)
              OR (%(obligation)s::text IS NULL AND i->>'hub_id' = ANY(%(hubs)s)
                  AND i->>'reason_code' = %(manual_reason)s)), 0) AS kw
FROM og.trace t
LEFT JOIN og.verdict v ON v.command_batch_id = (t.payload->>'command_batch_id')::uuid
LEFT JOIN LATERAL jsonb_array_elements(t.payload->'items') i ON true
WHERE t.event_class = 'RT_ALLOCATION' AND t.created_at >= %(a)s AND t.created_at < %(b)s
  AND t.stream_id = ANY(%(streams)s)
GROUP BY 1, 2, 3, 4
"""  # noqa: S608 -- _BIN is a fixed module literal

_METER_SQL = f"""
SELECT {_BIN.format(col="ts")} AS b, product AS bank_id, avg(value)::float8 AS v
FROM og.feed_obs
WHERE source = 'scada' AND series = 'REAL_POWER_KW' AND quality = 'GOOD'
  AND product = ANY(%(banks)s) AND ts >= %(a)s AND ts < %(b)s
GROUP BY 1, 2
"""  # noqa: S608 -- _BIN is a fixed module literal

_METER_BASELINE_SQL = """
SELECT sum(v)::float8 AS kw, count(*) AS n FROM (
    SELECT product, avg(value) AS v FROM og.feed_obs
    WHERE source = 'scada' AND series = 'REAL_POWER_KW' AND quality = 'GOOD'
      AND product = ANY(%(banks)s) AND ts >= %(a)s AND ts < %(b)s
    GROUP BY product
) m
"""

_BATTERY_BASELINE_SQL = """
SELECT sum(p)::float8 AS kw, count(*) AS n FROM (
    SELECT t.hub_id, avg(t.p_kw) AS p FROM og.telemetry t JOIN og.hub h USING (hub_id)
    WHERE h.bank_id = ANY(%(banks)s) AND t.ts >= %(a)s AND t.ts < %(b)s
    GROUP BY t.hub_id
) s
"""


@dataclass(frozen=True, slots=True)
class SliceData:
    """Raw per-bucket reads for one slice, keyed by bucket start."""

    shares: Mapping[tuple[datetime, str], tuple[float, float]]
    hub_kw: Mapping[tuple[datetime, str, str], float]
    commanded: Mapping[datetime, list[tuple[str, str | None, float]]]
    meter_kw: Mapping[tuple[datetime, str], float]


def _commanded(rows: Sequence[tuple[str, str | None, float]]) -> tuple[float, int, int]:
    """(commanded kW signed, proposed cycles, vetoed cycles) for one bucket's batches."""
    per_stream: dict[str, list[float]] = defaultdict(list)
    proposed = vetoed = 0
    for stream, outcome, kw in rows:
        per_stream[stream].append(kw if outcome == "PASS" else 0.0)
        if kw != 0.0:
            proposed += 1
            vetoed += 1 if outcome in VETO_OUTCOMES else 0
    return sum(sum(v) / len(v) for v in per_stream.values()), proposed, vetoed


def _delivered(spec: CallSpec, data: SliceData, start: datetime) -> float | None:
    if spec.hub_ids and spec.obligation_id is None:
        hubs = [kw for (b, _bank, hub), kw in data.hub_kw.items() if b == start and hub in spec.hub_ids]
        if len(hubs) < max(1, len(spec.hub_ids) // 2):
            return None
        return min(sum(hubs), 0.0)
    net: dict[str, float] = defaultdict(float)
    for (b, bank, _hub), kw in data.hub_kw.items():
        if b == start:
            net[bank] += kw
    delivered = 0.0
    for bank in spec.bank_ids:
        ob_kw, tot_kw = data.shares.get((start, bank), (0.0, 0.0))
        if ob_kw <= 0.0:
            continue
        if bank not in net:
            return None
        delivered += attributed_discharge_kw(net[bank], ob_kw, tot_kw)
    return -delivered if (delivered or net) else None


def _meter_deltas(
    spec: CallSpec, data: SliceData, start: datetime, baselines: tuple[float | None, float | None]
) -> tuple[float | None, float | None]:
    meter_base, battery_base = baselines
    if not spec.meter_bank_ids or meter_base is None or battery_base is None:
        return None, None
    meters = [data.meter_kw.get((start, bank)) for bank in spec.meter_bank_ids]
    battery = sum(
        kw for (b, bank, _h), kw in data.hub_kw.items() if b == start and bank in spec.meter_bank_ids
    )
    has_battery = any(b == start and bank in spec.meter_bank_ids for (b, bank, _h) in data.hub_kw)
    meter = None if any(m is None for m in meters) else sum(m for m in meters if m is not None) - meter_base
    return meter, (battery - battery_base) if has_battery else None


def assemble_buckets(
    spec: CallSpec,
    data: SliceData,
    starts: Sequence[datetime],
    bucket_s: float,
    baselines: tuple[float | None, float | None] = (None, None),
) -> list[DeliveryBucket]:
    """Pure: one `DeliveryBucket` per start from the slice's raw reads."""
    out: list[DeliveryBucket] = []
    for start in starts:
        commanded, proposed, vetoed = _commanded(data.commanded.get(start, []))
        meter, battery = _meter_deltas(spec, data, start, baselines)
        out.append(
            DeliveryBucket(
                start=start,
                end=start + timedelta(seconds=bucket_s),
                committed_kw=spec.committed_kw,
                commanded_kw=commanded,
                delivered_kw=_delivered(spec, data, start),
                proposed_cycles=proposed,
                vetoed_cycles=vetoed,
                meter_delta_kw=meter,
                battery_delta_kw=battery,
            )
        )
    return out


def bucket_starts(origin: datetime, a: datetime, b: datetime, bucket_s: float) -> list[datetime]:
    """Bucket starts aligned to `origin` that lie fully inside `[a, b)`."""
    step = timedelta(seconds=bucket_s)
    k = max(0, int(((a - origin).total_seconds() + bucket_s - 1e-9) // bucket_s))
    starts: list[datetime] = []
    t = origin + k * step
    while t + step <= b:
        starts.append(t)
        t += step
    return starts


async def _rows(conn: AsyncConnection[Any], sql: str, params: Mapping[str, Any]) -> list[tuple[Any, ...]]:
    async with conn.cursor() as cur:
        await cur.execute(sql, dict(params))
        return list(await cur.fetchall())


async def read_slice(
    conn: AsyncConnection[Any], spec: CallSpec, a: datetime, b: datetime, bucket_s: float
) -> SliceData:
    """The raw reads for `[a, b)` (bucket starts aligned to the call's window start)."""
    base = {"a": a, "b": b, "bucket_s": bucket_s, "origin": spec.window_start}
    banks = sorted(set(spec.bank_ids) | set(spec.meter_bank_ids))
    by_hub = bool(spec.hub_ids) and spec.obligation_id is None
    shares: dict[tuple[datetime, str], tuple[float, float]] = {}
    if spec.obligation_id is not None:
        rows = await _rows(conn, _SHARES_SQL, {**base, "obligation_id": spec.obligation_id, "banks": banks})
        shares = {(r[0], str(r[1])): (float(r[2] or 0.0), float(r[3] or 0.0)) for r in rows}
    tel = await _rows(
        conn, _TELEMETRY_SQL, {**base, "by_hub": by_hub, "hubs": list(spec.hub_ids), "banks": banks}
    )
    commanded: dict[datetime, list[tuple[str, str | None, float]]] = defaultdict(list)
    cmd_params = {
        **base,
        "obligation": str(spec.obligation_id) if spec.obligation_id else None,
        "hubs": list(spec.hub_ids),
        "manual_reason": R_MANUAL_RAMP,
        "streams": [f"allocator-{bank}" for bank in spec.bank_ids],
    }
    for r in await _rows(conn, _COMMANDED_SQL, cmd_params):
        commanded[r[0]].append((str(r[1]), r[3], float(r[4] or 0.0)))
    meter: dict[tuple[datetime, str], float] = {}
    if spec.meter_bank_ids:
        rows = await _rows(conn, _METER_SQL, {**base, "banks": list(spec.meter_bank_ids)})
        meter = {(r[0], str(r[1])): float(r[2]) for r in rows}
    return SliceData(
        shares=shares,
        hub_kw={(r[0], str(r[1]), str(r[2])): float(r[3] or 0.0) for r in tel},
        commanded=commanded,
        meter_kw=meter,
    )


async def read_baselines(
    conn: AsyncConnection[Any], spec: CallSpec, baseline_s: float
) -> tuple[float | None, float | None]:
    """(meter, battery) average kW on the metered banks over `baseline_s` before the call; None when any
    metered bank has no meter reading, or no hub reported."""
    if not spec.meter_bank_ids:
        return None, None
    params = {
        "banks": list(spec.meter_bank_ids),
        "a": spec.window_start - timedelta(seconds=baseline_s),
        "b": spec.window_start,
    }
    meter_rows = await _rows(conn, _METER_BASELINE_SQL, params)
    battery_rows = await _rows(conn, _BATTERY_BASELINE_SQL, params)
    meter_kw, meter_n = meter_rows[0] if meter_rows else (None, 0)
    battery_kw, battery_n = battery_rows[0] if battery_rows else (None, 0)
    meter = float(meter_kw) if meter_kw is not None and int(meter_n) == len(spec.meter_bank_ids) else None
    battery = float(battery_kw) if battery_kw is not None and int(battery_n) > 0 else None
    return meter, battery
