"""`opengrid.assets.runner.run_once` -- the periodic drift-evaluation sweep (07-delivery/06 S5.5.1-4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from opengrid.assets import runner
from opengrid.assets.ports import DriftObservationWindow
from opengrid.core.pq import OffsetVector

from .conftest import make_asset_record

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
ZERO_OFFSET = OffsetVector(freq_hz=0.0, voltage_pct=0.0, phase_deg=0.0)


def _quiet_window() -> DriftObservationWindow:
    return DriftObservationWindow(
        exceeded_per_summary=[False] * 10,
        correlates_with_fleet_event=False,
        latest_measured_offset=ZERO_OFFSET,
    )


def _drifting_window() -> DriftObservationWindow:
    return DriftObservationWindow(
        exceeded_per_summary=[True] * 10,
        correlates_with_fleet_event=False,
        latest_measured_offset=ZERO_OFFSET,
    )


async def test_run_once_no_hubs_is_a_no_op(service, fakes):
    result = await runner.run_once(service, now=NOW)
    assert result.evaluated == 0
    assert result.calibrations_requested == 0
    assert result.work_orders_opened == 0
    assert result.errors == 0


async def test_run_once_evaluates_every_hub_and_requests_calibration_on_watch(service, fakes):
    fakes.asset_health.records["hub-1"] = make_asset_record(hub_id="hub-1", since=NOW)
    fakes.asset_health.records["hub-2"] = make_asset_record(hub_id="hub-2", since=NOW)
    fakes.drift.windows["hub-1"] = _drifting_window()
    fakes.drift.windows["hub-2"] = _quiet_window()

    result = await runner.run_once(service, now=NOW)

    assert result.evaluated == 2
    assert result.calibrations_requested == 1
    assert fakes.asset_health.records["hub-1"].asset_state == "WATCH"
    assert fakes.asset_health.records["hub-2"].asset_state == "OK"
    assert len(fakes.calibration_attempts.attempts) == 1


async def test_run_once_skips_calibration_request_while_sensitive_grant_active(service, fakes):
    fakes.asset_health.records["hub-1"] = make_asset_record(hub_id="hub-1", since=NOW)
    fakes.drift.windows["hub-1"] = _drifting_window()
    fakes.sensitive_grants.sensitive_hubs.add("hub-1")

    result = await runner.run_once(service, now=NOW)

    assert result.evaluated == 1
    assert result.calibrations_requested == 0
    assert fakes.asset_health.records["hub-1"].asset_state == "WATCH"  # still moved to WATCH


async def test_run_once_opens_work_order_for_degraded_hub_without_one(service, fakes):
    fakes.asset_health.records["hub-1"] = make_asset_record(
        hub_id="hub-1", asset_state="DEGRADED", since=NOW - timedelta(hours=1)
    )
    fakes.drift.windows["hub-1"] = _drifting_window()  # observation window content irrelevant for DEGRADED

    result = await runner.run_once(service, now=NOW)

    assert result.work_orders_opened == 1
    assert "hub-1" in fakes.work_orders.open_orders


async def test_run_once_does_not_duplicate_an_existing_open_work_order(service, fakes):
    fakes.asset_health.records["hub-1"] = make_asset_record(
        hub_id="hub-1", asset_state="DEGRADED", since=NOW - timedelta(hours=1)
    )
    fakes.drift.windows["hub-1"] = _drifting_window()
    await fakes.work_orders.open("hub-1", severity="HIGH", evidence={}, opened_at=NOW)

    result = await runner.run_once(service, now=NOW)

    assert result.work_orders_opened == 0
    assert len(fakes.work_orders.open_orders) == 1


async def test_run_once_continues_after_one_hub_evaluation_fails(service, fakes, monkeypatch):
    fakes.asset_health.records["hub-1"] = make_asset_record(hub_id="hub-1", since=NOW)
    fakes.asset_health.records["hub-2"] = make_asset_record(hub_id="hub-2", since=NOW)
    fakes.drift.windows["hub-2"] = _quiet_window()

    async def boom(hub_id: str) -> DriftObservationWindow | None:
        if hub_id == "hub-1":
            raise RuntimeError("boom")
        return _quiet_window()

    monkeypatch.setattr(fakes.drift, "observation_window", boom)

    result = await runner.run_once(service, now=NOW)

    assert result.errors == 1
    assert result.evaluated == 1  # only hub-2 completed
