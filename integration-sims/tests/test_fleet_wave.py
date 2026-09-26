"""Tests for ogsim.fleet.wave (WP-H: waveform summary/raw generation and
publication, 06-service-profiles-and-power-quality.md S6.4/S7.4)."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime

import numpy as np
import pytest

from ogsim.common.config import MqttSettings, load_fleet_config
from ogsim.common.schemas import validate
from ogsim.fleet.pq import InverterSnapshot
from ogsim.fleet.runtime import FleetEngine
from ogsim.fleet.wave import (
    CAPTURE_CYCLES,
    SAMPLE_RATE_HZ,
    SAMPLES_PER_CAPTURE,
    HarmonicDetailScheduler,
    RotatingAuditSampler,
    SummaryScheduler,
    WaveConfig,
    build_summary_message,
    capture_request_expired,
    channels_for_phase_connection,
    current_rms_a,
    hub_phase_connection,
    set_current_rms,
    synthesize_raw_capture,
)

MQTT = MqttSettings(host="127.0.0.1", port=1883, username="og_sim", password="", topic_root="og/v1")
CONFIG = WaveConfig()


def _snapshot(
    unit_id: str = "hub-00000-inv0",
    hub_id: str = "hub-00000",
    phase_connection: str = "A",
    freq_hz: float = 60.0,
    voltage_offset_pct: float = 0.0,
    thd_current_pct: float = 2.0,
    phase_angle_error_deg: float = 0.0,
    dominant_harmonics: dict[int, dict[str, float]] | None = None,
) -> InverterSnapshot:
    return InverterSnapshot(
        unit_id=unit_id,
        hub_id=hub_id,
        phase_connection=phase_connection,
        freq_hz=freq_hz,
        voltage_offset_pct=voltage_offset_pct,
        thd_current_pct=thd_current_pct,
        dominant_harmonics=dominant_harmonics or {3: {"mag_pct": 1.2, "angle_deg": 30.0}},
        phase_angle_error_deg=phase_angle_error_deg,
        response_time_ms=200.0,
        ride_through_class="CATEGORY_III",
        quality_score=0.95,
    )


@pytest.fixture
def engine() -> FleetEngine:
    config = replace(
        load_fleet_config(), mqtt=MQTT, hub_count=8, bank_count=2, zones=("LZ_NORTH", "LZ_SOUTH")
    )
    return FleetEngine(config, seed=7)


# ---------------------------------------------------------------------------
# channels / phase-connection helpers
# ---------------------------------------------------------------------------


def test_channels_for_1p_hub() -> None:
    assert channels_for_phase_connection("B") == ["V", "I"]


def test_channels_for_split_phase_hub() -> None:
    assert channels_for_phase_connection("AB") == ["V_A", "V_B", "I_A", "I_B"]


def test_channels_for_3p_hub() -> None:
    assert channels_for_phase_connection("ABC") == ["V_A", "V_B", "V_C", "I_A", "I_B", "I_C"]


@pytest.mark.parametrize(
    ("legs", "expected"),
    [
        (["A"], "A"),
        (["A", "A"], "A"),
        (["A", "B"], "AB"),
        (["B", "A"], "AB"),
        (["B", "C"], "BC"),
        (["C", "A"], "CA"),
        (["A", "B", "C"], "ABC"),
    ],
)
def test_hub_phase_connection_canonical(legs: list[str], expected: str) -> None:
    assert hub_phase_connection(legs) == expected


# ---------------------------------------------------------------------------
# summary message
# ---------------------------------------------------------------------------


def test_summary_message_validates_against_schema_without_harmonics() -> None:
    snapshot = _snapshot()
    msg = build_summary_message(
        "hub-00000",
        "bank-000",
        "LZ_NORTH",
        [snapshot],
        "2026-09-26T00:00:00.000Z",
        include_harmonics=False,
        config=CONFIG,
    )
    set_current_rms(msg, snapshot.phase_connection, current_rms_a(5.0))
    validate("pq_waveform_summary", msg)
    assert msg["harmonics_v"] is None if "harmonics_v" in msg else True
    assert msg["v_rms_a"] > 0
    assert msg["i_rms_a"] == pytest.approx(current_rms_a(5.0))


def test_summary_message_includes_harmonics_when_due() -> None:
    snapshot = _snapshot()
    msg = build_summary_message(
        "hub-00000",
        "bank-000",
        "LZ_NORTH",
        [snapshot],
        "2026-09-26T00:00:00.000Z",
        include_harmonics=True,
        config=CONFIG,
    )
    set_current_rms(msg, snapshot.phase_connection, current_rms_a(5.0))
    validate("pq_waveform_summary", msg)
    assert msg["harmonics_i"]["3"]["mag_pct"] == pytest.approx(1.2)


def test_summary_message_merges_dual_unit_legs() -> None:
    unit_a = _snapshot(unit_id="hub-00004-inv0", hub_id="hub-00004", phase_connection="A")
    unit_b = _snapshot(unit_id="hub-00004-inv1", hub_id="hub-00004", phase_connection="B")
    msg = build_summary_message(
        "hub-00004",
        "bank-000",
        "LZ_NORTH",
        [unit_a, unit_b],
        "2026-09-26T00:00:00.000Z",
        include_harmonics=False,
        config=CONFIG,
    )
    set_current_rms(msg, unit_a.phase_connection, current_rms_a(3.0))
    set_current_rms(msg, unit_b.phase_connection, current_rms_a(4.0))
    validate("pq_waveform_summary", msg)
    assert msg["v_rms_a"] > 0
    assert msg["v_rms_b"] > 0
    assert msg.get("v_rms_c") is None


def test_current_rms_a_floors_near_zero_dispatch() -> None:
    assert current_rms_a(0.0) > 0.0


# ---------------------------------------------------------------------------
# harmonic-detail scheduler
# ---------------------------------------------------------------------------


def test_harmonic_detail_scheduler_due_first_call() -> None:
    scheduler = HarmonicDetailScheduler(interval_s=30.0, delta_pct=1.0)
    assert scheduler.due("hub-00000", now=0.0, thd_current_pct=2.0) is True


def test_harmonic_detail_scheduler_not_due_before_interval_or_delta() -> None:
    scheduler = HarmonicDetailScheduler(interval_s=30.0, delta_pct=1.0)
    assert scheduler.due("hub-00000", now=0.0, thd_current_pct=2.0) is True
    assert scheduler.due("hub-00000", now=5.0, thd_current_pct=2.05) is False


def test_harmonic_detail_scheduler_due_on_time_elapsed() -> None:
    scheduler = HarmonicDetailScheduler(interval_s=30.0, delta_pct=1.0)
    scheduler.due("hub-00000", now=0.0, thd_current_pct=2.0)
    assert scheduler.due("hub-00000", now=30.0, thd_current_pct=2.0) is True


def test_harmonic_detail_scheduler_due_on_thd_delta() -> None:
    scheduler = HarmonicDetailScheduler(interval_s=30.0, delta_pct=1.0)
    scheduler.due("hub-00000", now=0.0, thd_current_pct=2.0)
    assert scheduler.due("hub-00000", now=5.0, thd_current_pct=3.5) is True


# ---------------------------------------------------------------------------
# summary scheduler (S9 wave-2 fix: gates the summary message itself)
# ---------------------------------------------------------------------------


def test_summary_scheduler_due_first_call() -> None:
    scheduler = SummaryScheduler(interval_s=10.0, delta_pct=1.0)
    assert scheduler.due("hub-00000", now=0.0, thd_current_pct=2.0) is True


def test_summary_scheduler_not_due_before_interval_or_delta() -> None:
    scheduler = SummaryScheduler(interval_s=10.0, delta_pct=1.0)
    assert scheduler.due("hub-00000", now=0.0, thd_current_pct=2.0) is True
    assert scheduler.due("hub-00000", now=2.0, thd_current_pct=2.05) is False


def test_summary_scheduler_due_on_interval_elapsed() -> None:
    scheduler = SummaryScheduler(interval_s=10.0, delta_pct=1.0)
    scheduler.due("hub-00000", now=0.0, thd_current_pct=2.0)
    assert scheduler.due("hub-00000", now=10.0, thd_current_pct=2.0) is True


def test_summary_scheduler_due_on_deadband_change() -> None:
    scheduler = SummaryScheduler(interval_s=10.0, delta_pct=1.0)
    scheduler.due("hub-00000", now=0.0, thd_current_pct=2.0)
    assert scheduler.due("hub-00000", now=2.0, thd_current_pct=3.5) is True


def test_summary_scheduler_is_independent_of_harmonic_detail_scheduler() -> None:
    """Two separate instances with different cadences never share bookkeeping."""
    summary_gate = SummaryScheduler(interval_s=10.0, delta_pct=1.0)
    harmonic_gate = HarmonicDetailScheduler(interval_s=30.0, delta_pct=1.0)
    assert summary_gate.due("hub-00000", now=0.0, thd_current_pct=2.0) is True
    assert harmonic_gate.due("hub-00000", now=0.0, thd_current_pct=2.0) is True
    assert summary_gate.due("hub-00000", now=10.0, thd_current_pct=2.0) is True
    assert harmonic_gate.due("hub-00000", now=10.0, thd_current_pct=2.0) is False


def test_summary_scheduler_message_rate_at_scale() -> None:
    """S9 wave-2 build report's headline number: at the default 10s cadence, publishing
    only once per hub per interval caps steady-state summary throughput at
    hub_count / interval_s messages/s (~200/s at 2,000 hubs, ~1,000/s at 10,000 hubs) --
    down from the old ~1 message per hub per telemetry tick (2s)."""
    scheduler = SummaryScheduler(interval_s=10.0, delta_pct=1.0)
    hub_ids = [f"hub-{i:05d}" for i in range(2000)]
    due_at_t0 = sum(1 for hub_id in hub_ids if scheduler.due(hub_id, now=0.0, thd_current_pct=2.0))
    due_at_t5 = sum(1 for hub_id in hub_ids if scheduler.due(hub_id, now=5.0, thd_current_pct=2.0))
    assert due_at_t0 == len(hub_ids)  # first call is always due, per-hub
    assert due_at_t5 == 0  # interval not yet elapsed and no deadband change


# ---------------------------------------------------------------------------
# rotating audit sampler
# ---------------------------------------------------------------------------


def test_rotating_audit_sampler_bounds_rate() -> None:
    sampler = RotatingAuditSampler(sample_pct_per_min=1.0)
    hub_ids = [f"hub-{i:05d}" for i in range(10_000)]
    due = sampler.due_hub_ids(hub_ids, now=0.0)
    # ~1% of 10,000 = ~100; allow generous slack since this is a hash-based sample.
    assert 20 <= len(due) <= 300


def test_rotating_audit_sampler_zero_pct_never_due() -> None:
    sampler = RotatingAuditSampler(sample_pct_per_min=0.0)
    assert sampler.due_hub_ids(["hub-00000"], now=0.0) == []


def _ticks_per_minute(telemetry_interval_s: float) -> list[float]:
    """Simulates one minute's worth of `run_fleet` tick timestamps at the given cadence."""
    n = int(60.0 / telemetry_interval_s)
    return [i * telemetry_interval_s for i in range(n)]


def test_rotating_audit_sampler_does_not_repeat_a_hub_within_the_same_minute() -> None:
    """Regression (post-deploy defect: raw captures at ~400/min against the ~20/min cap
    at 2,000 hubs). `ogsim.fleet.runtime.run_fleet` calls `due_hub_ids` every telemetry
    tick (2 s default => 30 ticks/min), not once a minute -- before the fix, the same
    ~1% of hubs was re-emitted on every one of those 30 ticks because `due_hub_ids`'s
    hash result only depends on `(hub_id, minute_bucket)`, not on whether it was already
    reported this bucket."""
    sampler = RotatingAuditSampler(sample_pct_per_min=1.0)
    hub_ids = [f"hub-{i:05d}" for i in range(2000)]

    emitted: list[str] = []
    for now in _ticks_per_minute(telemetry_interval_s=2.0):
        emitted.extend(sampler.due_hub_ids(hub_ids, now=now))

    # Each due hub must appear exactly once across the whole minute, however many ticks
    # touched that minute bucket.
    assert len(emitted) == len(set(emitted))


@pytest.mark.parametrize("hub_count", [2000, 10_000])
def test_rotating_audit_capture_rate_per_minute_at_scale(hub_count: int) -> None:
    """S6.4b's cap: at most ~1% of the fleet per minute, independent of telemetry
    cadence. Ticks a full simulated minute at the real 2 s default cadence and asserts
    the TOTAL captures emitted that minute (not per-tick) stays within generous slack of
    1% of the fleet -- this is the exact rate the lead's defect report measured against
    the live fleet (~400/min observed vs. ~20/min expected at 2,000 hubs)."""
    sampler = RotatingAuditSampler(sample_pct_per_min=1.0)
    hub_ids = [f"hub-{i:05d}" for i in range(hub_count)]

    emitted: set[str] = set()
    for now in _ticks_per_minute(telemetry_interval_s=2.0):
        emitted.update(sampler.due_hub_ids(hub_ids, now=now))

    expected = hub_count * 0.01
    assert len(emitted) <= expected * 3  # generous slack, hash-based sample
    # The old (buggy) behaviour would have emitted up to 30x this per minute (one full
    # pass per tick) -- assert we are nowhere near that to guard against a regression.
    assert len(emitted) < expected * 10


def test_rotating_audit_sampler_rotates_over_minutes() -> None:
    sampler = RotatingAuditSampler(sample_pct_per_min=1.0)
    hub_ids = [f"hub-{i:05d}" for i in range(2_000)]
    due_minute_0 = set(sampler.due_hub_ids(hub_ids, now=0.0))
    due_minute_1 = set(sampler.due_hub_ids(hub_ids, now=60.0))
    assert due_minute_0 != due_minute_1


# ---------------------------------------------------------------------------
# raw waveform capture
# ---------------------------------------------------------------------------


def test_synthesize_raw_capture_shape_and_schema_1p() -> None:
    snapshot = _snapshot()
    msg = synthesize_raw_capture(
        "hub-00000",
        "A",
        [snapshot],
        output_kw=5.0,
        trigger_reason="API_REQUEST",
        ts="2026-09-26T00:00:00.000Z",
        config=CONFIG,
    )
    validate("pq_waveform_raw", msg)
    assert msg["channels"] == ["V", "I"]
    assert len(msg["samples"]["V"]) == SAMPLES_PER_CAPTURE
    assert len(msg["samples"]["I"]) == SAMPLES_PER_CAPTURE
    assert all(-32768 <= v <= 32767 for v in msg["samples"]["V"])


def test_synthesize_raw_capture_shape_3p() -> None:
    units = [
        _snapshot(unit_id=f"hub-00010-inv{leg}", hub_id="hub-00010", phase_connection=leg)
        for leg in ("A", "B", "C")
    ]
    msg = synthesize_raw_capture(
        "hub-00010",
        "ABC",
        units,
        output_kw=10.0,
        trigger_reason="ROTATING_AUDIT",
        ts="2026-09-26T00:00:00.000Z",
        config=CONFIG,
    )
    validate("pq_waveform_raw", msg)
    assert set(msg["samples"].keys()) == {"V_A", "V_B", "V_C", "I_A", "I_B", "I_C"}
    assert msg["sample_rate_hz"] == SAMPLE_RATE_HZ
    assert msg["cycles"] == CAPTURE_CYCLES


def test_synthesize_raw_capture_harmonic_content_matches_seeded_thd() -> None:
    """Closes the S7.4 loop referenced by TS-15b: the synthesized current waveform's
    THD, independently recomputed via FFT, matches the seeded `dominant_harmonics`
    within quantization tolerance."""
    snapshot = _snapshot(thd_current_pct=4.0, dominant_harmonics={3: {"mag_pct": 4.0, "angle_deg": 0.0}})
    msg = synthesize_raw_capture(
        "hub-00000",
        "A",
        [snapshot],
        output_kw=11.0,
        trigger_reason="API_REQUEST",
        ts="2026-09-26T00:00:00.000Z",
        config=CONFIG,
    )
    current = np.array(msg["samples"]["I"], dtype=np.float64)
    fundamental_amplitude = current.max()
    fft = np.fft.rfft(current)
    freqs = np.fft.rfftfreq(current.size, d=1.0 / SAMPLE_RATE_HZ)
    fundamental_bin = np.argmin(np.abs(freqs - 60.0))
    third_bin = np.argmin(np.abs(freqs - 180.0))
    ratio_pct = 100.0 * abs(fft[third_bin]) / abs(fft[fundamental_bin])
    assert ratio_pct == pytest.approx(4.0, rel=0.15)
    assert fundamental_amplitude > 0


def test_capture_request_expired_true_past_deadline() -> None:
    request = {
        "request_id": str(uuid.uuid4()),
        "hub_id": "hub-00000",
        "trigger_reason": "API_REQUEST",
        "issued_at": "2026-09-26T00:00:00.000Z",
        "expires_at": "2026-09-26T00:00:05.000Z",
    }
    epoch = datetime(2026, 9, 26, tzinfo=UTC).timestamp()
    assert capture_request_expired(request, epoch + 10.0) is True
    assert capture_request_expired(request, epoch + 1.0) is False


# ---------------------------------------------------------------------------
# FleetEngine integration
# ---------------------------------------------------------------------------


def test_engine_wave_summary_messages_validate(engine: FleetEngine) -> None:
    engine.tick(0.0)
    messages = engine.wave_summary_messages(0.0)
    assert len(messages) == engine.config.hub_count
    for suffix, msg in messages:
        assert suffix.startswith("scada/wave/") and suffix.endswith("/summary")
        validate("pq_waveform_summary", msg)


def test_engine_wave_summary_suppressed_for_offline_hub(engine: FleetEngine) -> None:
    hub_id = engine.state.hub_ids[0]
    engine.handle_scenario_cmd(
        {
            "id": "anom-offline",
            "target": {"kind": "hub", "ref": hub_id},
            "type": "FLEET_HUB_OFFLINE",
            "params": {},
            "start": datetime.fromtimestamp(0.0, tz=UTC).isoformat(),
            "duration_s": 10.0,
        }
    )
    engine.tick(1.0)
    messages = engine.wave_summary_messages(1.0)
    suffixes = [suffix for suffix, _ in messages]
    assert not any(hub_id in suffix for suffix in suffixes)


def test_engine_handle_wave_capture_request_round_trip(engine: FleetEngine) -> None:
    hub_id = engine.state.hub_ids[0]
    engine.tick(0.0)
    now = 1_000_000.0
    request = {
        "request_id": str(uuid.uuid4()),
        "hub_id": hub_id,
        "trigger_reason": "API_REQUEST",
        "issued_at": datetime.fromtimestamp(now - 1.0, tz=UTC).isoformat(),
        "expires_at": datetime.fromtimestamp(now + 5.0, tz=UTC).isoformat(),
    }
    result = engine.handle_wave_capture_request(request, now)
    assert result is not None
    suffix, msg = result
    assert suffix == f"scada/wave/{engine.state.zones[0]}/{engine.state.bank_ids[0]}/{hub_id}/raw"
    validate("pq_waveform_raw", msg)
    assert msg["trigger_reason"] == "API_REQUEST"


def test_engine_handle_wave_capture_request_expired_returns_none(engine: FleetEngine) -> None:
    hub_id = engine.state.hub_ids[0]
    now = 1_000_000.0
    request = {
        "request_id": str(uuid.uuid4()),
        "hub_id": hub_id,
        "trigger_reason": "API_REQUEST",
        "issued_at": datetime.fromtimestamp(now - 10.0, tz=UTC).isoformat(),
        "expires_at": datetime.fromtimestamp(now - 5.0, tz=UTC).isoformat(),
    }
    assert engine.handle_wave_capture_request(request, now) is None


def test_engine_handle_wave_capture_request_unknown_hub_returns_none(engine: FleetEngine) -> None:
    now = 1_000_000.0
    request = {
        "request_id": str(uuid.uuid4()),
        "hub_id": "hub-99999",
        "trigger_reason": "API_REQUEST",
        "issued_at": datetime.fromtimestamp(now - 1.0, tz=UTC).isoformat(),
        "expires_at": datetime.fromtimestamp(now + 5.0, tz=UTC).isoformat(),
    }
    assert engine.handle_wave_capture_request(request, now) is None


def test_engine_wave_rotating_audit_captures_validate(engine: FleetEngine) -> None:
    engine.tick(0.0)
    captures = engine.wave_rotating_audit_captures(0.0)
    for suffix, msg in captures:
        assert suffix.endswith("/raw")
        validate("pq_waveform_raw", msg)
        assert msg["trigger_reason"] == "ROTATING_AUDIT"


def test_engine_wave_rotating_audit_bounded_at_2000_hubs() -> None:
    """Bandwidth-bound check (S6.4b): at the default 1%/min audit rate and the
    fleet's real 2,000-hub scale, at most ~1% of hubs self-trigger a raw capture in
    a single tick."""
    config = replace(load_fleet_config(), mqtt=MQTT, hub_count=2000, bank_count=40)
    engine = FleetEngine(config, seed=1)
    engine.tick(0.0)
    captures = engine.wave_rotating_audit_captures(0.0)
    assert len(captures) <= 60  # generous slack over the ~20-hub expectation (1% of 2000)
