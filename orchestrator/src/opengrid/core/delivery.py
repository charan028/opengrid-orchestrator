"""Delivery verification (D-38): was a discharge call's committed kW actually DELIVERED, measured from
telemetry and aligned to the call window. Pure: no I/O. The one owner of this computation; the operator
API, the customer API (utility calls) and the grid link read its results, never re-derive them.

Sign convention (one end to end, as `opengrid.core.manual_targets`): **+charge / -discharge** for every kW
value on a `DeliveryBucket` (committed, commanded, delivered). Energy is reported as discharged kWh
(positive). A bucket's `delivered_kw` of `None` means no telemetry (DATA_STALE), never zero.

Invariants: this module observes; it never changes dispatch (K7: a measured shortfall never stops a firm
obligation; D-17 best effort continues). Settlement still bills actual delivered energy (D-17).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from opengrid.core.manual_targets import SIGN_CONVENTION

__all__ = [
    "SIGN_CONVENTION",
    "DeliveryBucket",
    "DeliveryMetrics",
    "DeliveryPolicy",
    "DeliveryReason",
    "DeliveryResult",
    "MeterCheck",
    "MeterStatus",
    "attributed_discharge_kw",
    "corroborate_meter",
    "verify_delivery",
]

_SECONDS_PER_HOUR = 3600.0


class DeliveryResult(StrEnum):
    IN_PROGRESS = "IN_PROGRESS"
    PASS = "PASS"  # noqa: S105 -- a result name, not a credential
    PARTIAL = "PARTIAL"
    FAIL = "FAIL"


class DeliveryReason(StrEnum):
    RAMP_TOO_SLOW = "RAMP_TOO_SLOW"
    SUSTAIN_BELOW_TARGET = "SUSTAIN_BELOW_TARGET"
    NO_DELIVERY = "NO_DELIVERY"
    VETOED = "VETOED"
    STOPPED = "STOPPED"
    DATA_STALE = "DATA_STALE"
    ENERGY_SHORT = "ENERGY_SHORT"


class MeterStatus(StrEnum):
    CORROBORATED = "CORROBORATED"
    UNCORROBORATED = "UNCORROBORATED"
    NO_METER = "NO_METER"
    METER_STALE = "METER_STALE"


@dataclass(frozen=True, slots=True)
class DeliveryPolicy:
    """Tolerances for one call. `target_frac`: delivered must reach this share of committed kW;
    `ramp_time_s`: within this long of the call start (the product's ramp time)."""

    target_frac: float = 0.95
    ramp_time_s: float = 600.0
    sustain_pass_pct: float = 95.0
    shortfall_alert_s: float = 60.0
    none_alert_s: float = 60.0
    none_threshold_frac: float = 0.05
    stale_max_frac: float = 0.10
    fail_energy_frac: float = 0.5
    veto_frac: float = 0.5
    meter_tolerance_frac: float = 0.10
    meter_floor_kw: float = 25.0
    meter_min_coverage_frac: float = 0.5


@dataclass(frozen=True, slots=True)
class DeliveryBucket:
    """One aligned interval of a call. kW values are signed +charge/-discharge. `meter_delta_kw` and
    `battery_delta_kw` are the independent meter's and the aggregate battery telemetry's change from their
    pre-call baselines over the metered banks (`None` when unmeasured)."""

    start: datetime
    end: datetime
    committed_kw: float
    commanded_kw: float | None
    delivered_kw: float | None
    proposed_cycles: int = 0
    vetoed_cycles: int = 0
    meter_delta_kw: float | None = None
    battery_delta_kw: float | None = None

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass(frozen=True, slots=True)
class DeliveryMetrics:
    result: DeliveryResult
    reasons: tuple[DeliveryReason, ...]
    reached_target: bool
    time_to_target_s: float | None
    sustained_pct: float | None
    lowest_kw: float | None
    lowest_at: datetime | None
    lowest_run_s: float
    longest_below_s: float
    current_below_s: float
    commanded_without_delivery_s: float
    discharged_kwh: float
    committed_kwh: float
    commanded_kw_avg: float
    delivered_kw_avg: float | None
    stale_frac: float


@dataclass(frozen=True, slots=True)
class MeterCheck:
    status: MeterStatus
    mismatch_frac: float | None
    meter_kwh: float | None
    battery_kwh: float | None


def _discharge(kw: float | None) -> float | None:
    return None if kw is None else max(-kw, 0.0)


def attributed_discharge_kw(
    bank_net_kw: float, obligation_granted_kw: float, total_granted_kw: float
) -> float:
    """An obligation's share of its bank's measured discharge (positive kW): the bank's discharge
    (charging counts as 0) times the obligation's share of the bank's granted kW. The same attribution
    settlement meters (`opengrid.settle.pg_backend._FETCH_TELEMETRY_SQL`); 0 without a grant."""
    if total_granted_kw <= 0.0 or obligation_granted_kw <= 0.0:
        return 0.0
    return max(-bank_net_kw, 0.0) * min(obligation_granted_kw / total_granted_kw, 1.0)


def _runs(flags: Sequence[tuple[bool, float]]) -> tuple[float, float]:
    """(longest, trailing) run in seconds of consecutive True flags; `(flag, seconds)` pairs in order."""
    longest = current = 0.0
    for flag, seconds in flags:
        current = current + seconds if flag else 0.0
        longest = max(longest, current)
    return longest, current


@dataclass(frozen=True, slots=True)
class _Timeline:
    buckets: tuple[DeliveryBucket, ...]
    targets: tuple[float, ...]
    reach_index: int | None
    sustain_from: int


def _timeline(buckets: Sequence[DeliveryBucket], policy: DeliveryPolicy, call_start: datetime) -> _Timeline:
    ordered = tuple(sorted(buckets, key=lambda b: b.start))
    targets = tuple(policy.target_frac * max(-b.committed_kw, 0.0) for b in ordered)
    reach = next(
        (
            i
            for i, b in enumerate(ordered)
            if (d := _discharge(b.delivered_kw)) is not None and targets[i] > 0 and d >= targets[i]
        ),
        None,
    )
    deadline_index = next(
        (i for i, b in enumerate(ordered) if (b.start - call_start).total_seconds() >= policy.ramp_time_s),
        len(ordered),
    )
    sustain_from = min(reach, deadline_index) if reach is not None else deadline_index
    return _Timeline(ordered, targets, reach, sustain_from)


def _lowest(tl: _Timeline) -> tuple[float | None, datetime | None, float]:
    window = [(i, b) for i, b in enumerate(tl.buckets) if i >= tl.sustain_from and b.delivered_kw is not None]
    if not window:
        window = [(i, b) for i, b in enumerate(tl.buckets) if b.delivered_kw is not None]
    if not window:
        return None, None, 0.0
    index, low = min(window, key=lambda item: _discharge(item[1].delivered_kw) or 0.0)
    low_kw = _discharge(low.delivered_kw) or 0.0
    band = max(0.02 * max(-low.committed_kw, 0.0), 1.0)

    def near(b: DeliveryBucket) -> bool:
        d = _discharge(b.delivered_kw)
        return d is not None and d <= low_kw + band

    run = low.seconds
    for step in (-1, 1):
        j = index + step
        while 0 <= j < len(tl.buckets) and near(tl.buckets[j]):
            run += tl.buckets[j].seconds
            j += step
    return -low_kw, low.start, run


def _reasons(
    tl: _Timeline, policy: DeliveryPolicy, m: dict[str, float | None], *, stopped: bool, elapsed_s: float
) -> list[DeliveryReason]:
    reasons: list[DeliveryReason] = []
    if (m["stale_frac"] or 0.0) > policy.stale_max_frac:
        reasons.append(DeliveryReason.DATA_STALE)
    ttt = m["time_to_target_s"]
    if (ttt is not None and ttt > policy.ramp_time_s) or (ttt is None and elapsed_s >= policy.ramp_time_s):
        reasons.append(DeliveryReason.RAMP_TOO_SLOW)
    sustained = m["sustained_pct"]
    if sustained is not None and sustained < policy.sustain_pass_pct:
        reasons.append(DeliveryReason.SUSTAIN_BELOW_TARGET)
    proposed = sum(b.proposed_cycles for b in tl.buckets)
    vetoed = sum(b.vetoed_cycles for b in tl.buckets)
    if proposed > 0 and vetoed / proposed >= policy.veto_frac:
        reasons.append(DeliveryReason.VETOED)
    committed_avg = sum(t for t in tl.targets) / policy.target_frac / len(tl.targets) if tl.targets else 0.0
    delivered_avg = m["delivered_kw_avg"]
    if committed_avg > 0 and (
        delivered_avg is None or delivered_avg <= policy.none_threshold_frac * committed_avg
    ):
        reasons.append(DeliveryReason.NO_DELIVERY)
    committed_kwh = m["committed_kwh"] or 0.0
    if committed_kwh > 0 and (m["discharged_kwh"] or 0.0) < policy.fail_energy_frac * committed_kwh:
        reasons.append(DeliveryReason.ENERGY_SHORT)
    if stopped:
        reasons.append(DeliveryReason.STOPPED)
    return reasons


def _result(reasons: Sequence[DeliveryReason], *, final: bool, all_stale: bool) -> DeliveryResult:
    if not final:
        return DeliveryResult.IN_PROGRESS
    if all_stale or DeliveryReason.NO_DELIVERY in reasons or DeliveryReason.ENERGY_SHORT in reasons:
        return DeliveryResult.FAIL
    return DeliveryResult.PARTIAL if reasons else DeliveryResult.PASS


def verify_delivery(
    buckets: Sequence[DeliveryBucket],
    policy: DeliveryPolicy,
    *,
    call_start: datetime,
    final: bool,
    stopped: bool = False,
) -> DeliveryMetrics:
    """Evaluate a call's aligned buckets (only complete ones: the caller cuts at `now`/call end).

    - time to target: from `call_start` to the end of the first bucket delivering >= `target_frac` x
      committed; RAMP_TOO_SLOW when later than `ramp_time_s` (or not reached once that much has elapsed);
    - sustained compliance: % of measured buckets at or above target from the earlier of reaching target
      and the ramp deadline; SUSTAIN_BELOW_TARGET under `sustain_pass_pct`;
    - lowest point in that window and how long delivery stayed near it;
    - delivered vs committed kWh; NO_DELIVERY / ENERGY_SHORT / all data stale => FAIL;
    - VETOED when at least `veto_frac` of the proposed command cycles were vetoed; STOPPED by a safe stop.
    `final=False` returns IN_PROGRESS with the provisional reasons (for live alerts)."""
    tl = _timeline(buckets, policy, call_start)
    total = len(tl.buckets)
    known = [(i, b) for i, b in enumerate(tl.buckets) if b.delivered_kw is not None]
    reach_end = tl.buckets[tl.reach_index].end if tl.reach_index is not None else None
    in_window = [(i, b) for i, b in known if i >= tl.sustain_from and tl.targets[i] > 0]
    at_target = [i for i, b in in_window if (_discharge(b.delivered_kw) or 0.0) >= tl.targets[i]]
    below_flags = [
        (
            i >= tl.sustain_from
            and b.delivered_kw is not None
            and (_discharge(b.delivered_kw) or 0.0) < tl.targets[i],
            b.seconds,
        )
        for i, b in enumerate(tl.buckets)
    ]
    longest_below, current_below = _runs(below_flags)
    none_limit = [policy.none_threshold_frac * t / policy.target_frac for t in tl.targets]
    none_flags = [
        (
            (_discharge(b.commanded_kw) or 0.0) > none_limit[i]
            and b.delivered_kw is not None
            and (_discharge(b.delivered_kw) or 0.0) <= none_limit[i],
            b.seconds,
        )
        for i, b in enumerate(tl.buckets)
    ]
    _, commanded_without_delivery = _runs(none_flags)
    discharged_kwh = (
        sum((_discharge(b.delivered_kw) or 0.0) * b.seconds for _, b in known) / _SECONDS_PER_HOUR
    )
    committed_kwh = sum(max(-b.committed_kw, 0.0) * b.seconds for b in tl.buckets) / _SECONDS_PER_HOUR
    elapsed_s = sum(b.seconds for b in tl.buckets)
    metrics: dict[str, float | None] = {
        "stale_frac": (total - len(known)) / total if total else 0.0,
        "time_to_target_s": (reach_end - call_start).total_seconds() if reach_end is not None else None,
        "sustained_pct": 100.0 * len(at_target) / len(in_window) if in_window else None,
        "delivered_kw_avg": (
            sum(_discharge(b.delivered_kw) or 0.0 for _, b in known) / len(known) if known else None
        ),
        "discharged_kwh": discharged_kwh,
        "committed_kwh": committed_kwh,
    }
    reasons = _reasons(tl, policy, metrics, stopped=stopped, elapsed_s=elapsed_s) if total else []
    lowest_kw, lowest_at, lowest_run = _lowest(tl)
    commanded = [_discharge(b.commanded_kw) or 0.0 for b in tl.buckets]
    return DeliveryMetrics(
        result=_result(reasons, final=final, all_stale=total > 0 and not known),
        reasons=tuple(reasons),
        reached_target=tl.reach_index is not None,
        time_to_target_s=metrics["time_to_target_s"],
        sustained_pct=metrics["sustained_pct"],
        lowest_kw=lowest_kw,
        lowest_at=lowest_at,
        lowest_run_s=lowest_run,
        longest_below_s=longest_below,
        current_below_s=current_below,
        commanded_without_delivery_s=commanded_without_delivery,
        discharged_kwh=discharged_kwh,
        committed_kwh=committed_kwh,
        commanded_kw_avg=-(sum(commanded) / len(commanded)) if commanded else 0.0,
        delivered_kw_avg=-metrics["delivered_kw_avg"] if metrics["delivered_kw_avg"] is not None else None,
        stale_frac=metrics["stale_frac"] or 0.0,
    )


def corroborate_meter(buckets: Sequence[DeliveryBucket], policy: DeliveryPolicy) -> MeterCheck:
    """Compare the independent meter's change against aggregate battery telemetry's change over the call
    (both from pre-call baselines, so a feeder's background load cancels). UNCORROBORATED when the energy
    differs by more than `meter_tolerance_frac` of the battery energy (floored at `meter_floor_kw` over
    the call); NO_METER without any meter reading, METER_STALE under `meter_min_coverage_frac` coverage."""
    measured = [b for b in buckets if b.battery_delta_kw is not None]
    both = [b for b in measured if b.meter_delta_kw is not None]
    if not any(b.meter_delta_kw is not None for b in buckets):
        return MeterCheck(MeterStatus.NO_METER, None, None, None)
    if not measured or len(both) / len(measured) < policy.meter_min_coverage_frac:
        return MeterCheck(MeterStatus.METER_STALE, None, None, None)
    meter_kwh = sum(-(b.meter_delta_kw or 0.0) * b.seconds for b in both) / _SECONDS_PER_HOUR
    battery_kwh = sum(-(b.battery_delta_kw or 0.0) * b.seconds for b in both) / _SECONDS_PER_HOUR
    floor_kwh = policy.meter_floor_kw * sum(b.seconds for b in both) / _SECONDS_PER_HOUR
    mismatch = abs(meter_kwh - battery_kwh) / max(abs(battery_kwh), floor_kwh)
    status = (
        MeterStatus.UNCORROBORATED if mismatch > policy.meter_tolerance_frac else MeterStatus.CORROBORATED
    )
    return MeterCheck(status, mismatch, meter_kwh, battery_kwh)
