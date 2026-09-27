"""Unit tests for opengrid.delivery (D-38): series assembly from raw reads, call discovery parsing, the
record, the live alert facts and rules. They assert MEASURED delivery, never grants."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from opengrid.core.delivery import (
    DeliveryPolicy,
    DeliveryResult,
    MeterStatus,
    corroborate_meter,
    verify_delivery,
)
from opengrid.delivery.job import DeliverySettings, alert_facts, build_record
from opengrid.delivery.models import CallKind, CallSpec, SeriesPoint
from opengrid.delivery.series import SliceData, assemble_buckets, bucket_starts
from opengrid.delivery.store import live_point, spec_from_deployment, spec_from_manual
from opengrid.health.delivery_rules import (
    ALR_DELIVERY_NONE,
    ALR_DELIVERY_RAMP_LATE,
    ALR_DELIVERY_SHORTFALL,
    evaluate_delivery_alerts,
)
from opengrid.platform.config import Config

T0 = datetime(2026, 9, 27, 21, 30, tzinfo=UTC)
BANK = "bank-sub-LZ_AEN-00"
HUB = "sub-LZ_AEN-00"
STEP = 30.0


def _toll_spec(**kw: object) -> CallSpec:
    base = {
        "call_id": str(uuid4()),
        "call_kind": CallKind.UTILITY_CALL,
        "window_start": T0,
        "window_end": T0 + timedelta(minutes=15),
        "committed_kw": -20_000.0,
        "product": "TOLLING",
        "obligation_id": uuid4(),
        "bank_ids": (BANK,),
        "meter_bank_ids": (BANK,),
    }
    base.update(kw)
    return CallSpec(**base)  # type: ignore[arg-type]


def _starts(n: int) -> list[datetime]:
    return [T0 + timedelta(seconds=i * STEP) for i in range(n)]


def test_bucket_starts_are_aligned_to_the_call_and_only_complete() -> None:
    starts = bucket_starts(T0, T0 + timedelta(seconds=10), T0 + timedelta(seconds=100), STEP)

    assert starts == [T0 + timedelta(seconds=30), T0 + timedelta(seconds=60)]


def test_toll_delivery_is_the_measured_discharge_attributed_by_grant_share() -> None:
    spec = _toll_spec()
    starts = _starts(2)
    data = SliceData(
        shares={(starts[0], BANK): (20_000.0, 20_000.0), (starts[1], BANK): (20_000.0, 20_000.0)},
        hub_kw={(starts[0], BANK, HUB): -4_000.0, (starts[1], BANK, HUB): -19_500.0},
        commanded={
            starts[0]: [(f"allocator-{BANK}", "PASS", -6_000.0)],
            starts[1]: [(f"allocator-{BANK}", "PASS", -20_000.0)],
        },
        meter_kw={(starts[0], BANK): -4_000.0, (starts[1], BANK): -19_400.0},
    )

    buckets = assemble_buckets(spec, data, starts, STEP, baselines=(0.0, 0.0))

    assert [b.delivered_kw for b in buckets] == [-4_000.0, -19_500.0]
    assert [b.commanded_kw for b in buckets] == [-6_000.0, -20_000.0]
    assert buckets[1].meter_delta_kw == pytest.approx(-19_400.0)
    assert buckets[1].battery_delta_kw == pytest.approx(-19_500.0)


def test_granted_but_vetoed_commands_nothing_and_delivers_nothing() -> None:
    """The r3.4 G-04 defect: grants exist every cycle, the guardian vetoes every batch, nothing flows."""
    spec = _toll_spec()
    starts = _starts(3)
    data = SliceData(
        shares={(s, BANK): (20_000.0, 20_000.0) for s in starts},
        hub_kw={(s, BANK, HUB): 0.0 for s in starts},
        commanded={s: [(f"allocator-{BANK}", "VETOED", -200.0)] * 15 for s in starts},
        meter_kw={},
    )

    buckets = assemble_buckets(spec, data, starts, STEP)

    assert all(b.delivered_kw == 0.0 and b.commanded_kw == 0.0 for b in buckets)
    assert all(b.proposed_cycles == 15 and b.vetoed_cycles == 15 for b in buckets)


def test_a_bank_with_a_grant_but_no_telemetry_is_stale_not_zero() -> None:
    spec = _toll_spec()
    start = _starts(1)
    data = SliceData(shares={(start[0], BANK): (20_000.0, 20_000.0)}, hub_kw={}, commanded={}, meter_kw={})

    (bucket,) = assemble_buckets(spec, data, start, STEP)

    assert bucket.delivered_kw is None


def test_a_shared_bank_attributes_only_the_obligations_share() -> None:
    spec = _toll_spec(bank_ids=("bank-040",), meter_bank_ids=())
    start = _starts(1)
    data = SliceData(
        shares={(start[0], "bank-040"): (100.0, 400.0)},
        hub_kw={(start[0], "bank-040", "hub-1"): -300.0, (start[0], "bank-040", "hub-2"): -100.0},
        commanded={},
        meter_kw={},
    )

    (bucket,) = assemble_buckets(spec, data, start, STEP)

    assert bucket.delivered_kw == pytest.approx(-100.0)


def test_manual_target_delivery_is_its_own_hubs_discharge() -> None:
    spec = CallSpec(
        call_id="t1",
        call_kind=CallKind.MANUAL_TARGET,
        window_start=T0,
        window_end=T0 + timedelta(minutes=5),
        committed_kw=-10.0,
        hub_ids=("hub-1", "hub-2"),
        bank_ids=("bank-001",),
    )
    start = _starts(1)
    data = SliceData(
        shares={},
        hub_kw={
            (start[0], "bank-001", "hub-1"): -5.0,
            (start[0], "bank-001", "hub-2"): -4.5,
            (start[0], "bank-001", "hub-3"): -9.0,
        },
        commanded={},
        meter_kw={},
    )

    (bucket,) = assemble_buckets(spec, data, start, STEP)

    assert bucket.delivered_kw == pytest.approx(-9.5)


def test_deployment_row_uses_the_calls_signed_request_else_the_full_commitment() -> None:
    row = {
        "deployment_id": uuid4(),
        "obligation_id": uuid4(),
        "start_at": T0,
        "end_at": T0 + timedelta(minutes=15),
        "cancelled_at": T0 + timedelta(minutes=5),
        "requested_kw": None,
        "committed_qty_kw": 24_000,
        "service_type": "REGULATED_CAPACITY",
        "contract_id": uuid4(),
        "customer_id": uuid4(),
        "utility_id": "AUSTIN_ENERGY",
        "variant": "tolling",
        "dispatch_call_id": None,
        "kind": None,
        "idempotency_key": None,
    }
    spec = spec_from_deployment(row)
    assert spec.committed_kw == -24_000.0 and spec.call_kind is CallKind.UTILITY_CALL
    assert spec.product == "TOLLING" and spec.effective_end == T0 + timedelta(minutes=5)

    partial = spec_from_deployment(
        {**row, "requested_kw": -5_000, "service_type": "ERCOT_AS", "variant": "ECRS"}
    )
    assert partial.committed_kw == -5_000.0 and partial.call_kind is CallKind.AS_DEPLOYMENT


def test_manual_rows_become_discharge_calls_only() -> None:
    payload = {
        "hub_ids": ["hub-1", "hub-2"],
        "p_kw_command": -5.0,
        "sign_convention": "+charge/-discharge",
        "issued_at": T0.isoformat(),
        "expires_at": (T0 + timedelta(minutes=10)).isoformat(),
    }
    spec = spec_from_manual("tid", payload, T0, T0 + timedelta(minutes=3))
    assert spec is not None and spec.committed_kw == -10.0 and spec.stopped_at == T0 + timedelta(minutes=3)
    assert spec_from_manual("tid", {**payload, "p_kw_command": 5.0}, T0, None) is None  # a charge target
    assert spec_from_manual("tid", {**payload, "sign_convention": "+discharge"}, T0, None) is None
    assert spec_from_manual("tid", {"cancels": "x", "hub_ids": ["hub-1"]}, T0, None) is None


def _series(delivered: list[float | None], committed: float = -20_000.0) -> list[SeriesPoint]:
    return [
        SeriesPoint(t=T0 + timedelta(seconds=i * STEP), s=STEP, c=committed, m=committed, d=d, p=1, v=0)
        for i, d in enumerate(delivered)
    ]


def _record(delivered: list[float | None], *, final: bool, ramp: float = 300.0):
    spec = _toll_spec()
    series = _series(delivered)
    policy = DeliveryPolicy(ramp_time_s=ramp)
    buckets = [p.bucket() for p in series]
    metrics = verify_delivery(buckets, policy, call_start=T0, final=final)
    return build_record(
        spec,
        series,
        metrics=metrics,
        meter=corroborate_meter(buckets, policy),
        ramp_time_s=ramp,
        baselines=(None, None),
        evaluated_to=T0 + timedelta(seconds=STEP * len(series)),
        final=final,
    )


def test_a_record_carries_measured_values_and_its_live_point() -> None:
    record = _record([-10_000.0] + [-19_800.0] * 20, final=True)

    assert record.result == DeliveryResult.PASS.value and record.delivered_kw_last == -19_800.0
    assert record.meter_status == MeterStatus.NO_METER.value
    point = live_point(record, now=T0 + timedelta(minutes=5))
    assert point.state == "ACTIVE" and point.delivered_kw == -19_800.0 and not point.stale


def test_live_alerts_ramp_late_shortfall_and_none() -> None:
    policy = DeliveryPolicy()
    late = alert_facts(_record([-1_000.0] * 12, final=False), policy)
    rules = {f.rule for f in evaluate_delivery_alerts(late)}
    assert ALR_DELIVERY_RAMP_LATE in rules and ALR_DELIVERY_SHORTFALL in rules

    none = alert_facts(_record([0.0] * 4, final=False), policy)
    assert ALR_DELIVERY_NONE in {f.rule for f in evaluate_delivery_alerts(none)}

    ok = alert_facts(_record([-19_900.0] * 12, final=False), policy)
    assert evaluate_delivery_alerts(ok) == []

    ended = alert_facts(_record([0.0] * 12, final=True), policy)
    assert evaluate_delivery_alerts(ended) == []  # live alerts are for running calls only


def test_settings_read_the_delivery_config_section() -> None:
    cfg = Config({"delivery": {"bucket_s": 20, "target_frac": 0.9, "ramp_time_s": {"tolling": 400}}})
    settings = DeliverySettings.from_config(cfg)

    assert settings.bucket_s == 20.0 and settings.policy.target_frac == 0.9
    assert settings.ramp_for("TOLLING") == 400.0 and settings.ramp_for("ECRS") == 600.0
    assert settings.ramp_for(None) == settings.default_ramp_time_s
