"""Delivered-power checks for the functional suites (decision D-38): a call's or an operator target's power
must REACH its target within tolerance and STAY there, measured from hub telemetry (`og.telemetry.p_kw`), never
inferred from grants or commands.

The verdict is `opengrid.core.delivery`'s own: its `DeliveryPolicy` tolerances, `verify_delivery` for time to
target and sustained compliance, and `attributed_discharge_kw` for an obligation's share of its banks' measured
discharge (the attribution settlement meters with). This module only reads rows and aligns them into the core's
`DeliveryBucket`s from the call's start. One sign convention, the core's: +charge / -discharge.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID

from e2e_stack import Stack, now_utc, wait_until

from opengrid.core.delivery import (
    DeliveryBucket,
    DeliveryMetrics,
    DeliveryPolicy,
    DeliveryReason,
    attributed_discharge_kw,
    verify_delivery,
)

#: The core's tolerances, unchanged (`DeliveryPolicy` defaults): delivered must reach 95% of the committed kW
#: within 600 s of the call's start, then stay at or above that in at least 95% of the measured buckets.
POLICY = DeliveryPolicy()
#: Aligned bucket length, as in the core's unit tests (orchestrator/tests/unit/core/test_delivery.py, 30 s). Hubs
#: report every 10 s, so each hub has samples in every bucket.
BUCKET_S = 30.0
#: A bucket is read only this long after it ends: one 10 s hub report, plus margin for the engine's periodic
#: telemetry flush (`opengrid.engine.persist_fleet_state`).
INGEST_LAG_S = 15.0
#: How long delivery is watched once the target is reached: the policy's `shortfall_alert_s` (60 s), so a
#: sustained delivery is one that holds at least that long.
SUSTAIN_S = POLICY.shortfall_alert_s
#: Minutes a call or target must run for `wait_delivered` to see the whole ramp window plus one sustain window
#: (12 with the defaults). A shorter call is judged only over the part that ran.
CHECK_MINUTES = math.ceil((POLICY.ramp_time_s + SUSTAIN_S + BUCKET_S + INGEST_LAG_S) / 60)
#: The core's reasons that mean "did not reach the target, or did not stay there". ENERGY_SHORT is left out on
#: purpose: it compares delivered with committed kWh (it FAILs a whole call), and this check stops one sustain
#: window after the target is reached, while the ramp's own deficit still weighs on the energy. STOPPED and VETOED
#: need inputs this check does not feed (a safe stop; the guardian's verdicts).
NOT_DELIVERED = frozenset(
    {
        DeliveryReason.RAMP_TOO_SLOW,
        DeliveryReason.SUSTAIN_BELOW_TARGET,
        DeliveryReason.NO_DELIVERY,
        DeliveryReason.DATA_STALE,
    }
)

#: Per aligned bucket and bank of the obligation: the bank's measured net power (each hub's samples averaged,
#: then summed over the bank's hubs; +charge / -discharge) and the obligation's and the bank's total grant (each
#: cycle's sum, averaged over the bucket's cycles). The banks and the aggregation are settlement's metering query
#: (`opengrid.settle.pg_backend._FETCH_TELEMETRY_SQL`, per minute there); the attribution itself is the core's
#: `attributed_discharge_kw`, applied per bank in Python.
_OBLIGATION_SQL = """
WITH banks AS (
    SELECT DISTINCT bank_id FROM og.reservation
    WHERE obligation_id = %(o)s AND interval_start < %(b)s AND interval_end > %(a)s
),
tel AS (
    SELECT floor(extract(epoch FROM t.ts - %(a)s)::float8 / %(step)s)::int AS k, h.bank_id, t.hub_id,
           avg(t.p_kw) AS p
    FROM og.telemetry t JOIN og.hub h USING (hub_id)
    WHERE h.bank_id IN (SELECT bank_id FROM banks) AND t.ts >= %(a)s AND t.ts < %(b)s AND t.p_kw IS NOT NULL
    GROUP BY 1, 2, 3
),
bank_tel AS (SELECT k, bank_id, sum(p) AS net_kw FROM tel GROUP BY 1, 2),
cyc AS (
    SELECT floor(extract(epoch FROM g.created_at - %(a)s)::float8 / %(step)s)::int AS k, g.cycle_id, g.bank_id,
           coalesce(sum(g.granted_kw) FILTER (WHERE g.obligation_id = %(o)s), 0) AS ob_kw,
           sum(g.granted_kw) AS tot_kw
    FROM og.grant g
    WHERE g.bank_id IN (SELECT bank_id FROM banks) AND g.created_at >= %(a)s AND g.created_at < %(b)s
    GROUP BY 1, 2, 3
),
share AS (SELECT k, bank_id, avg(ob_kw) AS ob_kw, avg(tot_kw) AS tot_kw FROM cyc GROUP BY 1, 2)
SELECT k, bank_id, t.net_kw, s.ob_kw, s.tot_kw
FROM bank_tel t FULL JOIN share s USING (k, bank_id)
"""

#: Per aligned bucket: one hub's measured power (its samples averaged; +charge / -discharge).
_HUB_SQL = """
SELECT floor(extract(epoch FROM ts - %(a)s)::float8 / %(step)s)::int AS k, avg(p_kw) AS p
FROM og.telemetry
WHERE hub_id = %(h)s AND ts >= %(a)s AND ts < %(b)s AND p_kw IS NOT NULL
GROUP BY 1
"""


def _complete(call_start: datetime, until: datetime) -> int:
    """How many whole buckets from `call_start` end at or before `until`."""
    return max(int((until - call_start).total_seconds() // BUCKET_S), 0)


def _bucket(
    call_start: datetime,
    i: int,
    *,
    committed_kw: float,
    commanded_kw: float | None,
    delivered_kw: float | None,
) -> DeliveryBucket:
    return DeliveryBucket(
        start=call_start + timedelta(seconds=i * BUCKET_S),
        end=call_start + timedelta(seconds=(i + 1) * BUCKET_S),
        committed_kw=committed_kw,
        commanded_kw=commanded_kw,
        delivered_kw=delivered_kw,
    )


def obligation_buckets(
    stack: Stack,
    obligation_id: UUID,
    *,
    committed_discharge_kw: float,
    call_start: datetime,
    until: datetime,
) -> list[DeliveryBucket]:
    """The complete buckets of a discharge call on one obligation (a toll call, an AS deployment). Committed: the
    called kW (`committed_discharge_kw`, a positive magnitude). Commanded: the obligation's grants. Delivered: its
    attributed share of each bank's measured discharge, summed over its banks; `None` (DATA_STALE, never 0) when
    none of its banks reported in that bucket."""
    n = _complete(call_start, until)
    if n == 0:
        return []
    rows = stack.rows(
        _OBLIGATION_SQL,
        {
            "o": obligation_id,
            "a": call_start,
            "b": call_start + timedelta(seconds=n * BUCKET_S),
            "step": BUCKET_S,
        },
    )
    granted: dict[int, float] = {}
    delivered: dict[int, float] = {}
    for row in rows:
        k = int(row["k"])
        ob_kw = float(row["ob_kw"] or 0.0)
        if row["tot_kw"] is not None:
            granted[k] = granted.get(k, 0.0) + ob_kw
        if row["net_kw"] is not None:
            delivered[k] = delivered.get(k, 0.0) + attributed_discharge_kw(
                float(row["net_kw"]), ob_kw, float(row["tot_kw"] or 0.0)
            )
    return [
        _bucket(
            call_start,
            i,
            committed_kw=-committed_discharge_kw,
            commanded_kw=-granted[i] if i in granted else None,
            delivered_kw=-delivered[i] if i in delivered else None,
        )
        for i in range(n)
    ]


def hub_buckets(
    stack: Stack, hub_id: str, *, target_kw: float, call_start: datetime, until: datetime
) -> list[DeliveryBucket]:
    """The complete buckets of an operator target on one hub. Committed: the target (signed, +charge /
    -discharge). Delivered: the hub's own measured power -- a hub under a live target is operator-owned, the
    allocator leaves it out (engine/manual.py), so no obligation shares it. Commanded is not read (the engine's
    ramp steps live in its batches): `None`."""
    n = _complete(call_start, until)
    if n == 0:
        return []
    measured = {
        int(row["k"]): float(row["p"])
        for row in stack.rows(
            _HUB_SQL,
            {
                "h": hub_id,
                "a": call_start,
                "b": call_start + timedelta(seconds=n * BUCKET_S),
                "step": BUCKET_S,
            },
        )
    }
    return [
        _bucket(call_start, i, committed_kw=target_kw, commanded_kw=None, delivered_kw=measured.get(i))
        for i in range(n)
    ]


def wait_delivered(
    buckets_of: Callable[[datetime], list[DeliveryBucket]],
    *,
    call_start: datetime,
    call_end: datetime | None = None,
    what: str,
) -> DeliveryMetrics:
    """Watch a running call until its delivered power has reached the target and held it for `SUSTAIN_S`, or
    until the policy's ramp window plus one sustain window (or the call's end, if sooner) has passed. Returns the
    core's metrics over the buckets watched, with `final=False`: the call is still running, so the result is
    IN_PROGRESS with the provisional reasons. `buckets_of(until)` returns the complete buckets up to `until`."""
    horizon = call_start + timedelta(seconds=POLICY.ramp_time_s + SUSTAIN_S + BUCKET_S)
    if call_end is not None:
        horizon = min(horizon, call_end)

    def settled() -> DeliveryMetrics | None:
        cutoff = min(now_utc() - timedelta(seconds=INGEST_LAG_S), horizon)
        buckets = buckets_of(cutoff)
        metrics = verify_delivery(buckets, POLICY, call_start=call_start, final=False)
        watched_s = len(buckets) * BUCKET_S
        held = metrics.time_to_target_s is not None and watched_s - metrics.time_to_target_s >= SUSTAIN_S
        return metrics if held or cutoff >= horizon else None

    return wait_until(
        settled,
        timeout_s=max((horizon - now_utc()).total_seconds(), 0.0) + INGEST_LAG_S + 60.0,
        interval_s=5.0,
        what=f"{what}: delivered power through its ramp and one sustain window",
    )


def assert_delivered(metrics: DeliveryMetrics, *, what: str) -> None:
    """Delivered power reached the policy's share of the target within its ramp time and stayed there."""
    missed = sorted(NOT_DELIVERED.intersection(metrics.reasons))
    if metrics.reached_target and not missed:
        return
    raise AssertionError(
        f"{what}: delivered power (telemetry) did not reach {POLICY.target_frac:.0%} of the target within "
        f"{POLICY.ramp_time_s:.0f} s and hold it in {POLICY.sustain_pass_pct:.0f}% of buckets (core.delivery, "
        f"D-38): reasons {missed or list(metrics.reasons)}, time to target {metrics.time_to_target_s} s, "
        f"sustained {metrics.sustained_pct}%, lowest {metrics.lowest_kw} kW held {metrics.lowest_run_s:.0f} s, "
        f"delivered avg {metrics.delivered_kw_avg} kW vs commanded avg {metrics.commanded_kw_avg:.1f} kW, "
        f"stale {metrics.stale_frac:.0%}"
    )
