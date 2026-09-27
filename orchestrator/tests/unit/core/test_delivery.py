"""Unit tests for opengrid.core.delivery (D-38): every result path asserts DELIVERED power, not grants."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from opengrid.core.delivery import (
    DeliveryBucket,
    DeliveryPolicy,
    DeliveryReason,
    DeliveryResult,
    MeterStatus,
    attributed_discharge_kw,
    corroborate_meter,
    verify_delivery,
)

T0 = datetime(2026, 9, 26, 21, 30, tzinfo=UTC)
STEP_S = 30
COMMITTED_KW = -20_000.0
POLICY = DeliveryPolicy(ramp_time_s=300.0)


def _buckets(delivered: list[float | None], *, commanded: float | None = COMMITTED_KW, **extra: int) -> list:
    return [
        DeliveryBucket(
            start=T0 + timedelta(seconds=i * STEP_S),
            end=T0 + timedelta(seconds=(i + 1) * STEP_S),
            committed_kw=COMMITTED_KW,
            commanded_kw=commanded,
            delivered_kw=d,
            **extra,
        )
        for i, d in enumerate(delivered)
    ]


def _ramp(n_ramp: int, n_hold: int, hold_kw: float = COMMITTED_KW) -> list[float | None]:
    return [COMMITTED_KW * (i + 1) / (n_ramp + 1) for i in range(n_ramp)] + [hold_kw] * n_hold


def test_pass_when_delivery_reaches_target_within_ramp_and_is_sustained() -> None:
    m = verify_delivery(_buckets(_ramp(6, 20)), POLICY, call_start=T0, final=True)

    assert m.result is DeliveryResult.PASS, m.reasons
    assert m.reached_target and m.time_to_target_s is not None and m.time_to_target_s <= 300
    assert m.sustained_pct == pytest.approx(100.0)
    assert m.discharged_kwh > 0.8 * m.committed_kwh


def test_ramp_too_slow_is_partial() -> None:
    m = verify_delivery(_buckets(_ramp(14, 20)), POLICY, call_start=T0, final=True)

    assert m.result is DeliveryResult.PARTIAL
    assert DeliveryReason.RAMP_TOO_SLOW in m.reasons
    assert m.time_to_target_s is not None and m.time_to_target_s > 300


def test_sustain_below_target_records_the_lowest_point_and_its_duration() -> None:
    delivered = _ramp(4, 10) + [-12_000.0] * 4 + [COMMITTED_KW] * 6
    m = verify_delivery(_buckets(delivered), POLICY, call_start=T0, final=True)

    assert m.result is DeliveryResult.PARTIAL
    assert DeliveryReason.SUSTAIN_BELOW_TARGET in m.reasons
    assert m.lowest_kw == pytest.approx(-12_000.0)
    assert m.lowest_run_s == pytest.approx(4 * STEP_S)
    assert m.longest_below_s == pytest.approx(4 * STEP_S)


def test_the_g04_veto_defect_reads_as_fail_vetoed_not_a_pass() -> None:
    """The r3.4 review defect: grants exist, but G-04 vetoes every batch from cycle 2, so no power flows."""
    m = verify_delivery(
        _buckets([-100.0] * 20, commanded=None, proposed_cycles=15, vetoed_cycles=15),
        POLICY,
        call_start=T0,
        final=True,
    )

    assert m.result is DeliveryResult.FAIL
    assert {DeliveryReason.VETOED, DeliveryReason.NO_DELIVERY, DeliveryReason.RAMP_TOO_SLOW} <= set(m.reasons)
    assert not m.reached_target


def test_commanded_but_nothing_delivered_is_no_delivery_and_tracked_live() -> None:
    m = verify_delivery(_buckets([0.0] * 6), POLICY, call_start=T0, final=False)

    assert m.result is DeliveryResult.IN_PROGRESS
    assert DeliveryReason.NO_DELIVERY in m.reasons
    assert m.commanded_without_delivery_s == pytest.approx(6 * STEP_S)


def test_all_stale_telemetry_fails_with_data_stale() -> None:
    m = verify_delivery(_buckets([None] * 12), POLICY, call_start=T0, final=True)

    assert m.result is DeliveryResult.FAIL
    assert DeliveryReason.DATA_STALE in m.reasons
    assert m.delivered_kw_avg is None


def test_some_stale_telemetry_is_partial_data_stale() -> None:
    delivered = _ramp(4, 20)
    delivered[10] = delivered[11] = delivered[12] = delivered[13] = None
    m = verify_delivery(_buckets(delivered), POLICY, call_start=T0, final=True)

    assert m.result is DeliveryResult.PARTIAL
    assert m.reasons == (DeliveryReason.DATA_STALE,)


def test_a_safe_stop_marks_the_call_stopped() -> None:
    m = verify_delivery(_buckets(_ramp(4, 10)), POLICY, call_start=T0, final=True, stopped=True)

    assert m.result is DeliveryResult.PARTIAL
    assert DeliveryReason.STOPPED in m.reasons


def test_energy_under_half_of_committed_fails() -> None:
    m = verify_delivery(_buckets(_ramp(4, 20, hold_kw=-8_000.0)), POLICY, call_start=T0, final=True)

    assert m.result is DeliveryResult.FAIL
    assert DeliveryReason.ENERGY_SHORT in m.reasons


def test_attributed_discharge_follows_the_grant_share_and_ignores_charging() -> None:
    assert attributed_discharge_kw(-1000.0, 250.0, 1000.0) == pytest.approx(250.0)
    assert attributed_discharge_kw(500.0, 250.0, 1000.0) == 0.0
    assert attributed_discharge_kw(-1000.0, 0.0, 0.0) == 0.0


def _metered(meter: float | None, battery: float | None, n: int = 10) -> list:
    return [
        DeliveryBucket(
            start=T0 + timedelta(seconds=i * STEP_S),
            end=T0 + timedelta(seconds=(i + 1) * STEP_S),
            committed_kw=COMMITTED_KW,
            commanded_kw=COMMITTED_KW,
            delivered_kw=COMMITTED_KW,
            meter_delta_kw=meter,
            battery_delta_kw=battery,
        )
        for i in range(n)
    ]


def test_meter_agreeing_with_telemetry_corroborates() -> None:
    check = corroborate_meter(_metered(-19_800.0, -20_000.0), POLICY)

    assert check.status is MeterStatus.CORROBORATED
    assert check.mismatch_frac is not None and check.mismatch_frac < 0.02


def test_meter_seeing_half_the_telemetry_discharge_is_uncorroborated() -> None:
    check = corroborate_meter(_metered(-10_000.0, -20_000.0), POLICY)

    assert check.status is MeterStatus.UNCORROBORATED
    assert check.mismatch_frac == pytest.approx(0.5)


def test_no_meter_and_stale_meter() -> None:
    assert corroborate_meter(_metered(None, -20_000.0), POLICY).status is MeterStatus.NO_METER
    stale = _metered(None, -20_000.0, n=10) + _metered(-20_000.0, -20_000.0, n=2)
    assert corroborate_meter(stale, POLICY).status is MeterStatus.METER_STALE
