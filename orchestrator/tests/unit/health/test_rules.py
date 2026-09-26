"""Unit tests for `opengrid.health.rules` (02b S6.4-S6.5): pure classification and alert-rule logic,
no DB/clock -- every case injects `now` explicitly (BUILD.md S5a "no flaky sleeps")."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import opengrid.fleet as fleet
from opengrid.core.models.platform import FeedStatus, Heartbeat
from opengrid.core.physics import HubParams
from opengrid.health.model import ALL_PROCESSES, HealthThresholds, HubHealthCounts, ProcessHealth
from opengrid.health.rules import (
    aggregate_hub_counts,
    classify_all_processes,
    classify_hub_health,
    derive_degraded_modes,
    evaluate_cycle_latency_alert,
    evaluate_cycle_latency_warning_alert,
    evaluate_energy_shortfall_risk_alert,
    evaluate_feed_alert,
    evaluate_guardian_timeout_alert,
    evaluate_hub_offline_ratio_alert,
    evaluate_process_down_alert,
    evaluate_reserve_breach_alert,
    evaluate_scada_overload_alert,
    evaluate_sim_offline_alert,
    is_fallback_feed_needed,
)
from opengrid.platform.config import Config

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)
THRESHOLDS = HealthThresholds()


def _heartbeat(process: str, age_s: float) -> Heartbeat:
    return Heartbeat(process=process, pid=1, ts=NOW - timedelta(seconds=age_s), status="ok")


# --- process heartbeats --------------------------------------------------------------------------


def test_process_within_miss_threshold_is_ok() -> None:
    heartbeats = [_heartbeat("engine", 1.0)]
    result = classify_all_processes(heartbeats, now=NOW, thresholds=THRESHOLDS)
    engine = next(p for p in result if p.process == "engine")
    assert engine.status == "ok"


def test_process_past_miss_threshold_is_down() -> None:
    heartbeats = [_heartbeat("engine", THRESHOLDS.heartbeat_down_after_s + 1)]
    result = classify_all_processes(heartbeats, now=NOW, thresholds=THRESHOLDS)
    engine = next(p for p in result if p.process == "engine")
    assert engine.status == "down"


def test_process_that_never_reported_is_down() -> None:
    result = classify_all_processes([], now=NOW, thresholds=THRESHOLDS)
    assert all(p.status == "down" for p in result)
    assert {p.process for p in result} == set(ALL_PROCESSES)


def test_sim_is_not_a_monitored_process() -> None:
    """Defect fix: `ogsim` is an external system that never writes an `og.heartbeat` row (BUILD.md S1,
    `ogsim` shares no code with `opengrid`), so it must not be in the heartbeat-monitored set -- expecting
    a "sim" row made `ALR-PROCESS-DOWN:sim` permanently open."""
    assert "sim" not in ALL_PROCESSES


def test_self_process_is_always_ok_regardless_of_its_heartbeat_row() -> None:
    """Defect fix: `settle` (health's own host process, 02b S1.2) must never classify itself as down --
    reaching this call at all proves it is alive, so a stale or missing `settle` heartbeat row (e.g. a
    transient DB hiccup on that very write) must not misreport it."""
    stale_heartbeat = [_heartbeat("settle", THRESHOLDS.heartbeat_down_after_s + 100)]
    result = classify_all_processes(stale_heartbeat, now=NOW, thresholds=THRESHOLDS, self_process="settle")
    settle = next(p for p in result if p.process == "settle")
    assert settle.status == "ok"
    assert settle.last_seen_at == NOW


def test_self_process_none_leaves_normal_classification() -> None:
    """Without `self_process`, a stale heartbeat still classifies as down (no special-casing by default)."""
    stale_heartbeat = [_heartbeat("settle", THRESHOLDS.heartbeat_down_after_s + 100)]
    result = classify_all_processes(stale_heartbeat, now=NOW, thresholds=THRESHOLDS)
    settle = next(p for p in result if p.process == "settle")
    assert settle.status == "down"


# --- hub health classification -------------------------------------------------------------------


def test_hub_online_within_two_telemetry_intervals() -> None:
    state = classify_hub_health(
        last_seen_at=NOW - timedelta(seconds=1), fault_code=None, now=NOW, thresholds=THRESHOLDS
    )
    assert state == "online"


def test_hub_stale_between_online_and_offline_thresholds() -> None:
    state = classify_hub_health(
        last_seen_at=NOW - timedelta(seconds=10), fault_code=None, now=NOW, thresholds=THRESHOLDS
    )
    assert state == "stale"


def test_hub_offline_past_offline_threshold() -> None:
    state = classify_hub_health(
        last_seen_at=NOW - timedelta(seconds=31), fault_code=None, now=NOW, thresholds=THRESHOLDS
    )
    assert state == "offline"


def test_hub_fault_overrides_timing() -> None:
    state = classify_hub_health(last_seen_at=NOW, fault_code="INVERTER_TRIP", now=NOW, thresholds=THRESHOLDS)
    assert state == "fault"


def test_fleet_hub_health_agrees_with_health_classify_hub_health() -> None:
    """R2 item 3 (merged): `opengrid.fleet` used to carry its own `_classify_health` copy of this same
    fault > offline > stale > online ladder, with its own separately-configured stale threshold -- by
    default health's online/stale boundary was `telemetry_interval_s * 2` (4.0s) while fleet's was a
    separately-configured `health.hub_stale_s` (6.0s), so a hub aged 5s classified "stale" via health but
    "online" via fleet. The live-path agent has since consolidated on this module's
    `classify_hub_health` as the ONE implementation: `opengrid.fleet.hub_health()` (its public
    classification lookup, TS-03-04) now imports and calls `classify_hub_health` directly with a
    `HealthThresholds` built from its own configured `hub_offline_s`/`telemetry_interval_s`.

    This is a consistency test of `fleet`'s public output (`hub_health()`) against `classify_hub_health`
    called directly with the same effective thresholds -- exercising fleet's real `configure()`/
    `hub_health()` entry points (not a private implementation detail), so it stays meaningful regardless
    of how `fleet` is internally wired to the shared classifier.
    """
    hub_offline_s = 30.0
    telemetry_interval_s = 3.0  # fleet.hub_online_s = telemetry_interval_s * 2 = 6.0s

    fleet.configure(
        backend=object(),  # type: ignore[arg-type] -- configure() only stores it, never calls it here
        cfg=Config(
            {
                "health": {"hub_offline_s": hub_offline_s},
                "fleet": {"telemetry_interval_s": telemetry_interval_s},
            }
        ),
    )
    health_thresholds = HealthThresholds(
        hub_offline_s=hub_offline_s, telemetry_interval_s=telemetry_interval_s
    )

    ages_s = [0.0, 1.0, 5.9, 6.0, 6.1, 15.0, 29.9, 30.0, 30.1, 120.0]
    for i, age_s in enumerate(ages_s):
        last_seen_at = NOW - timedelta(seconds=age_s)
        for fault_code in (None, "INVERTER_TRIP"):
            hub_id = f"hub-{i}-{fault_code}"
            fleet._hubs[hub_id] = fleet._HubRuntime(
                hub_id=hub_id,
                bank_id="bank-000",
                zone="LZ_NORTH",
                params=HubParams(e_kwh=39.2, r_kwh=7.84, p_kw=11.0),
                fault_code=fault_code,
                last_seen_at=last_seen_at,
            )
            fleet_state = fleet.hub_health(hub_id, now=NOW)
            health_state = classify_hub_health(
                last_seen_at=last_seen_at, fault_code=fault_code, now=NOW, thresholds=health_thresholds
            )
            assert fleet_state == health_state, f"age_s={age_s} fault_code={fault_code}"


def test_aggregate_hub_counts_rolls_up_by_zone() -> None:
    classified = [("LZ_NORTH", "online"), ("LZ_NORTH", "offline"), ("LZ_SOUTH", "fault")]
    counts = aggregate_hub_counts(classified)
    assert counts["LZ_NORTH"] == HubHealthCounts(online=1, offline=1)
    assert counts["LZ_SOUTH"] == HubHealthCounts(fault=1)
    assert counts["LZ_NORTH"].total == 2
    assert counts["LZ_SOUTH"].offline_ratio == 1.0


# --- degraded modes (02b S6.5) --------------------------------------------------------------------


def test_no_degraded_modes_when_everything_healthy() -> None:
    processes = [ProcessHealth(p, "ok", NOW) for p in ALL_PROCESSES]
    modes = derive_degraded_modes(feed_stale=False, process_health=processes)
    assert modes == frozenset()


def test_feed_stale_yields_no_new_commitments() -> None:
    processes = [ProcessHealth(p, "ok", NOW) for p in ALL_PROCESSES]
    modes = derive_degraded_modes(feed_stale=True, process_health=processes)
    assert modes == frozenset({"NO_NEW_COMMITMENTS"})


def test_engine_down_yields_hold_local_autonomy() -> None:
    processes = [ProcessHealth(p, "down" if p == "engine" else "ok", NOW) for p in ALL_PROCESSES]
    modes = derive_degraded_modes(feed_stale=False, process_health=processes)
    assert modes == frozenset({"HOLD_LOCAL_AUTONOMY"})


def test_guardian_down_yields_hold() -> None:
    processes = [ProcessHealth(p, "down" if p == "guardian" else "ok", NOW) for p in ALL_PROCESSES]
    modes = derive_degraded_modes(feed_stale=False, process_health=processes)
    assert modes == frozenset({"HOLD"})


def test_degraded_modes_can_combine() -> None:
    processes = [
        ProcessHealth(p, "down" if p in ("engine", "guardian") else "ok", NOW) for p in ALL_PROCESSES
    ]
    modes = derive_degraded_modes(feed_stale=True, process_health=processes)
    assert modes == frozenset({"NO_NEW_COMMITMENTS", "HOLD_LOCAL_AUTONOMY", "HOLD"})


# --- alert rules -----------------------------------------------------------------------------------


def _feed_status(*, last_value_at: datetime | None, breaker_open: bool = False) -> FeedStatus:
    return FeedStatus(
        source="ercot",
        product="price",
        last_value_at=last_value_at,
        last_success_at=last_value_at,
        consecutive_failures=0,
        breaker_open=breaker_open,
    )


def test_feed_alert_none_when_fresh() -> None:
    fs = _feed_status(last_value_at=NOW)
    assert evaluate_feed_alert(fs, now=NOW, thresholds=THRESHOLDS, staleness_threshold_s=60) is None


def test_feed_alert_warning_when_stale() -> None:
    fs = _feed_status(last_value_at=NOW - timedelta(seconds=120))
    finding = evaluate_feed_alert(fs, now=NOW, thresholds=THRESHOLDS, staleness_threshold_s=60)
    assert finding is not None
    assert finding.rule == "ALR-FEED-STALE"
    assert finding.severity == "warning"


def test_feed_alert_critical_when_breaker_open() -> None:
    fs = _feed_status(last_value_at=NOW, breaker_open=True)
    finding = evaluate_feed_alert(fs, now=NOW, thresholds=THRESHOLDS, staleness_threshold_s=60)
    assert finding is not None
    assert finding.rule == "ALR-FEED-LGV-EXHAUSTED"
    assert finding.severity == "critical"


def test_process_down_alert_only_when_down() -> None:
    assert evaluate_process_down_alert(ProcessHealth("engine", "ok", NOW)) is None


def test_energy_shortfall_risk_alert_shape() -> None:
    finding = evaluate_energy_shortfall_risk_alert(
        obligation_id="OBL-1", customer_id="CUST-A", margin_kwh=-3.5, time_to_depletion_h=0.75
    )
    assert finding.rule == "ALR-ENERGY-SHORTFALL-RISK"
    assert finding.severity == "critical"
    assert finding.condition_key == "ALR-ENERGY-SHORTFALL-RISK:OBL-1"
    assert finding.detail["obligation_id"] == "OBL-1"
    assert finding.detail["margin_kwh"] == -3.5
    assert finding.detail["time_to_depletion_h"] == 0.75


def test_energy_shortfall_risk_alert_handles_no_depletion_time() -> None:
    finding = evaluate_energy_shortfall_risk_alert(
        obligation_id="OBL-2", customer_id=None, margin_kwh=-1.0, time_to_depletion_h=None
    )
    assert "depletes in" not in finding.summary
    assert finding.detail["time_to_depletion_h"] is None
    finding = evaluate_process_down_alert(ProcessHealth("engine", "down", NOW))
    assert finding is not None
    assert finding.rule == "ALR-PROCESS-DOWN"
    assert finding.severity == "critical"


def test_hub_offline_ratio_alert_thresholds() -> None:
    assert evaluate_hub_offline_ratio_alert("Z", HubHealthCounts(online=100), thresholds=THRESHOLDS) is None
    warning = evaluate_hub_offline_ratio_alert(
        "Z", HubHealthCounts(online=93, offline=7), thresholds=THRESHOLDS
    )
    assert warning is not None
    assert warning.severity == "warning"
    critical = evaluate_hub_offline_ratio_alert(
        "Z", HubHealthCounts(online=70, offline=30), thresholds=THRESHOLDS
    )
    assert critical is not None
    assert critical.severity == "critical"


def test_cycle_latency_alert_requires_consecutive_breaches() -> None:
    assert evaluate_cycle_latency_alert(0.6, 1, thresholds=THRESHOLDS) is None  # not yet 3 in a row
    finding = evaluate_cycle_latency_alert(0.6, 3, thresholds=THRESHOLDS)
    assert finding is not None
    assert finding.rule == "ALR-CYCLE-P99"


def test_cycle_latency_alert_none_when_under_budget() -> None:
    assert evaluate_cycle_latency_alert(0.1, 5, thresholds=THRESHOLDS) is None
    assert evaluate_cycle_latency_alert(None, 5, thresholds=THRESHOLDS) is None


def test_cycle_latency_warning_alert_fires_at_warn_ratio_before_breach() -> None:
    """R2 pre-limit alert: THRESHOLDS.cycle_p99_warn_ratio=0.80 x budget_s=0.5 -> warns from 0.4s, no
    consecutive-cycle requirement (fires on the very first sample, unlike the hard `ALR-CYCLE-P99`)."""
    assert evaluate_cycle_latency_warning_alert(0.39, thresholds=THRESHOLDS) is None  # below 80%
    finding = evaluate_cycle_latency_warning_alert(0.45, thresholds=THRESHOLDS)
    assert finding is not None
    assert finding.rule == "ALR-CYCLE-P99-APPROACHING"
    assert finding.severity == "warning"


def test_cycle_latency_warning_alert_yields_to_hard_breach_rule() -> None:
    """Once p99 actually exceeds the budget, `ALR-CYCLE-P99` (the hard breach rule) owns it -- the
    pre-limit warning must not also fire, avoiding a redundant duplicate alert."""
    assert evaluate_cycle_latency_warning_alert(0.6, thresholds=THRESHOLDS) is None
    assert evaluate_cycle_latency_warning_alert(None, thresholds=THRESHOLDS) is None


def test_guardian_timeout_alert() -> None:
    assert evaluate_guardian_timeout_alert(0.005, thresholds=THRESHOLDS) is None
    finding = evaluate_guardian_timeout_alert(0.02, thresholds=THRESHOLDS)
    assert finding is not None
    assert finding.severity == "critical"


def test_reserve_breach_alert() -> None:
    assert evaluate_reserve_breach_alert(0) is None
    finding = evaluate_reserve_breach_alert(1)
    assert finding is not None
    assert finding.rule == "ALR-RESERVE-BREACH"
    assert finding.severity == "critical"


def test_scada_overload_alert_none_when_under_rating() -> None:
    assert evaluate_scada_overload_alert("bank-000", 50.0, 75.0, thresholds=THRESHOLDS) is None


def test_scada_overload_alert_none_when_no_reading_yet() -> None:
    assert evaluate_scada_overload_alert("bank-000", None, 75.0, thresholds=THRESHOLDS) is None


def test_scada_overload_alert_warning_at_rating() -> None:
    finding = evaluate_scada_overload_alert("bank-000", 80.0, 75.0, thresholds=THRESHOLDS)
    assert finding is not None
    assert finding.rule == "ALR-SCADA-OVERLOAD"
    assert finding.severity == "warning"
    assert finding.condition_key == "ALR-SCADA-OVERLOAD:bank-000"


def test_scada_overload_alert_critical_matches_default_anomaly_injection() -> None:
    """integration-sims' bank_overload anomaly defaults to 20% over rating -- must land critical."""
    finding = evaluate_scada_overload_alert("bank-000", 75.0 * 1.20 + 0.1, 75.0, thresholds=THRESHOLDS)
    assert finding is not None
    assert finding.severity == "critical"


# --- EIA fallback-feed suppression (defect fix) -----------------------------------------------------


def _ercot_np6_345(*, last_value_at: datetime | None, breaker_open: bool = False) -> FeedStatus:
    return FeedStatus(
        source="ERCOT", product="np6-345-cd", last_value_at=last_value_at, breaker_open=breaker_open
    )


def test_fallback_not_needed_when_primary_fresh_and_breaker_closed() -> None:
    primary = _ercot_np6_345(last_value_at=NOW)
    assert is_fallback_feed_needed(primary, now=NOW, primary_threshold_s=60) is False


def test_fallback_needed_when_primary_breaker_open() -> None:
    primary = _ercot_np6_345(last_value_at=NOW, breaker_open=True)
    assert is_fallback_feed_needed(primary, now=NOW, primary_threshold_s=60) is True


def test_fallback_needed_when_primary_stale() -> None:
    primary = _ercot_np6_345(last_value_at=NOW - timedelta(seconds=120))
    assert is_fallback_feed_needed(primary, now=NOW, primary_threshold_s=60) is True


def test_fallback_needed_when_primary_never_seen() -> None:
    assert is_fallback_feed_needed(None, now=NOW, primary_threshold_s=60) is True


# --- ALR-SIM-OFFLINE (defect fix) -------------------------------------------------------------------


def test_sim_offline_alert_none_when_no_data_yet() -> None:
    """Cold start (no fleet telemetry or SCADA reading has ever arrived) is not evidence of an offline
    sim -- only a signal that used to be fresh going stale is."""
    assert evaluate_sim_offline_alert(None, None, now=NOW, thresholds=THRESHOLDS) is None


def test_sim_offline_alert_none_when_either_signal_fresh() -> None:
    assert evaluate_sim_offline_alert(NOW, None, now=NOW, thresholds=THRESHOLDS) is None
    assert evaluate_sim_offline_alert(None, NOW, now=NOW, thresholds=THRESHOLDS) is None


def test_sim_offline_alert_critical_when_both_signals_stale() -> None:
    old = NOW - timedelta(seconds=THRESHOLDS.sim_offline_s + 1)
    finding = evaluate_sim_offline_alert(old, old, now=NOW, thresholds=THRESHOLDS)
    assert finding is not None
    assert finding.rule == "ALR-SIM-OFFLINE"
    assert finding.severity == "critical"
    assert finding.condition_key == "ALR-SIM-OFFLINE"
