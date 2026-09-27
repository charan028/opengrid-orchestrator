"""D-38 review fixes: one product normalisation (Non-Spin's three spellings get Non-Spin's ramp time), the
AT_RISK flags a restarted og-settle must reconcile, and the alerts of calls that ended while it was down,
including the meter-mismatch auto-clear once the meter agrees again."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from opengrid.core.models.platform import Alert
from opengrid.core.services import AS_MAX_DEPLOY_MINUTES, canonical_product
from opengrid.delivery import job as job_mod
from opengrid.delivery.job import DeliveryJob, DeliverySettings
from opengrid.platform.config import Config
from opengrid.ui.routes.dispatch import as_awards_view

NOW = datetime(2026, 9, 27, 22, 0, tzinfo=UTC)


@pytest.mark.parametrize("spelling", ["NSPIN", "NONSPIN", "NON_SPIN", "non-spin", " nspin "])
def test_every_non_spin_spelling_is_one_product(spelling: str) -> None:
    assert canonical_product(spelling) == "NSPIN"
    assert DeliverySettings().ramp_for(spelling) == 1800.0  # Non-Spin's ramp, never the 600 s default
    assert AS_MAX_DEPLOY_MINUTES[canonical_product(spelling) or ""] == 240


def test_config_ramp_keys_are_normalised_too() -> None:
    settings = DeliverySettings.from_config(Config({"delivery": {"ramp_time_s": {"NON_SPIN": 1500}}}))
    assert settings.ramp_for("NONSPIN") == 1500.0 and settings.ramp_for("ECRS") == 600.0
    assert canonical_product(None) is None and canonical_product("TOLLING") == "TOLLING"


@pytest.mark.parametrize("variant", ["NSPIN", "NONSPIN", "NON_SPIN"])
def test_the_dispatch_page_holds_non_spin_4_h_whatever_the_spelling(variant: str) -> None:
    (row,) = as_awards_view(
        [
            {
                "obligation_id": "AS-1",
                "service_type": "ERCOT_AS",
                "state": "COMMITTED",
                "variant": variant,
                "committed_qty_kw": 100,
            }
        ],
        [],
        now=NOW,
    )
    assert row["required_hours"] == 4 and row["max_minutes"] == 240


class _Pool:
    @asynccontextmanager
    async def connection(self):  # type: ignore[no-untyped-def]
        yield object()


class _Trace:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def append(
        self, stream_id: str, decision_type: str, event_class: str, payload: dict[str, Any], reasons=None
    ):  # type: ignore[no-untyped-def]
        self.events.append(event_class)


def _alert(alert_id: int, rule: str, **detail: Any) -> Alert:
    return Alert(id=alert_id, rule=rule, severity="critical", summary="s", detail=detail, opened_at=NOW)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "open_calls": [],
        "alerts": [],
        "cleared": [],
        "flags": [],
        "meter": {},
        "at_risk": [],
    }

    async def open_calls(_conn: Any, **_kw: Any) -> list[Any]:
        return state["open_calls"]

    async def fetch_open_records(_conn: Any, _ids: Any) -> dict[str, Any]:
        return {}

    async def fetch_open_alerts(_pool: Any) -> list[Alert]:
        return state["alerts"]

    async def clear_alert(_pool: Any, alert_id: int, **_kw: Any) -> None:
        state["cleared"].append(alert_id)

    async def flags(_conn: Any, **_kw: Any) -> list[Any]:
        return state["flags"]

    async def latest_meter(_conn: Any, banks: Any, **_kw: Any) -> dict[str, Any]:
        return {b: state["meter"][b] for b in banks if b in state["meter"]}

    monkeypatch.setattr(job_mod.store, "open_calls", open_calls)
    monkeypatch.setattr(job_mod.store, "fetch_open_records", fetch_open_records)
    monkeypatch.setattr(job_mod.store, "delivery_at_risk_flags", flags)
    monkeypatch.setattr(job_mod.store, "latest_meter_status", latest_meter)
    monkeypatch.setattr(job_mod, "fetch_open_alerts", fetch_open_alerts)
    monkeypatch.setattr(job_mod, "clear_alert", clear_alert)
    return state


def _job(state: dict[str, Any]) -> tuple[DeliveryJob, _Trace]:
    async def set_at_risk(obligation_id: Any, at_risk: bool, **kw: Any) -> None:
        state["at_risk"].append((obligation_id, at_risk, kw["reason_code"]))

    trace = _Trace()
    return DeliveryJob(_Pool(), trace, DeliverySettings(), set_at_risk=set_at_risk), trace  # type: ignore[arg-type]


async def test_a_restart_clears_the_at_risk_of_a_call_that_ended_and_adopts_a_short_running_one(
    world,
) -> None:
    ended, running = uuid4(), uuid4()
    world["flags"] = [(ended, "call-ended"), (running, "call-running")]
    world["alerts"] = [_alert(7, "ALR-DELIVERY-SHORTFALL", call_id="call-running")]
    world["open_calls"] = [type("Spec", (), {"call_id": "call-running"})()]
    job, _ = _job(world)
    monkey_verify_nothing(job)

    await job.run_once(NOW)

    assert world["at_risk"] == [(ended, False, "R-DELIVERY-RECOVERED")]
    assert "call-running" in job._flagged  # adopted: it clears when that call recovers
    await job.run_once(NOW)
    assert len(world["at_risk"]) == 1  # reconciled once, at startup


async def test_live_alerts_of_calls_that_are_no_longer_open_are_cleared(world) -> None:
    world["alerts"] = [
        _alert(1, "ALR-DELIVERY-RAMP-LATE", call_id="gone"),
        _alert(2, "ALR-DELIVERY-NONE", call_id="gone"),
        _alert(3, "ALR-DELIVERY-METER-MISMATCH", call_id="gone"),  # not a live alert: kept
    ]
    job, _ = _job(world)

    await job.run_once(NOW)

    assert sorted(world["cleared"]) == [1, 2]


async def test_a_meter_mismatch_clears_once_the_same_meter_agrees_on_a_later_call(world) -> None:
    end = (NOW - timedelta(hours=2)).isoformat()
    world["alerts"] = [
        _alert(
            9,
            "ALR-DELIVERY-METER-MISMATCH",
            call_id="c1",
            meter_bank_ids=["bank-sub-LZ_AEN-00"],
            window_end=end,
        )
    ]
    job, trace = _job(world)

    await job.run_once(NOW)
    assert world["cleared"] == []  # no later check yet

    world["meter"] = {"bank-sub-LZ_AEN-00": ("UNCORROBORATED", NOW - timedelta(hours=1))}
    await job.run_once(NOW)
    assert world["cleared"] == []  # the meter still disagrees

    world["meter"] = {"bank-sub-LZ_AEN-00": ("CORROBORATED", NOW - timedelta(minutes=5))}
    await job.run_once(NOW)
    assert world["cleared"] == [9] and "DELIVERY_ALERT_CLEARED" in trace.events


def monkey_verify_nothing(job: DeliveryJob) -> None:
    async def verify(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("not verified in this test")

    job._verify = verify  # type: ignore[method-assign]
