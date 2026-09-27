"""ogsim.utility_aen: schedule/weather, the five scenarios, the runtime (peak call, status follow-up,
disabled utility, /ogsim/ triggers) and the customer_api channel's wire mapping. A fake channel and a
fake clock: no orchestrator, no broker, no sleeping."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest

from ogsim.common.clock import FakeClock
from ogsim.control import catalogue
from ogsim.customer.api_client import HttpResult
from ogsim.utility_aen.channels import CHANNEL_FACTORIES, build_channel
from ogsim.utility_aen.channels.base import CallResult, CallSpec, ChannelError
from ogsim.utility_aen.channels.customer_api import API_PREFIX, CustomerApiChannel
from ogsim.utility_aen.config import (
    ScheduleConfig,
    UtilityConfigError,
    UtilitySimConfig,
    UtilitySpec,
    WeatherConfig,
    load_config,
)
from ogsim.utility_aen.runtime import UtilitySim
from ogsim.utility_aen.scenarios import SCENARIOS
from ogsim.utility_aen.schedule import LOCAL_TZ, day_weather, plan_for_day

DAY = date(2026, 9, 28)


@dataclass
class FakeChannel:
    """Mimics the orchestrator's checks just enough for the scenarios: sign, 90 min cap, overlap."""

    name: str = "fake"
    issued: list[CallSpec] = field(default_factory=list)
    active: dict[str, CallSpec] = field(default_factory=dict)
    cancelled: list[str] = field(default_factory=list)
    today: bool = True
    fail: bool = False

    async def obligations(self) -> dict[str, Any]:
        return {"today": {"obligation_id": "x"} if self.today else None}

    async def issue_call(self, spec: CallSpec) -> CallResult:
        if self.fail:
            raise ChannelError("unreachable")
        self.issued.append(spec)
        code = None
        if spec.kw >= 0:
            code = "R-CALL-CHARGE-REFUSED"
        elif spec.duration_min > 90:
            code = "R-CALL-DURATION-CAP"
        elif self.active:
            code = "R-CALL-OVERLAP"
        if code:
            return CallResult(spec.call_ref, False, "REFUSED", reason_code=code)
        self.active[spec.call_ref] = spec
        return CallResult(spec.call_ref, True, "ACTIVE", remote_id=str(uuid.uuid4()))

    async def cancel(self, call_ref: str, *, end_at: datetime | None = None) -> CallResult:
        self.cancelled.append(call_ref)
        self.active.pop(call_ref, None)
        return CallResult(call_ref, True, "COMPLETED")

    async def status(self, call_ref: str) -> CallResult:
        state = "ACTIVE" if call_ref in self.active else "COMPLETED"
        return CallResult(call_ref, True, state, granted_kw=-20_000.0, granted_kwh=10.0)


def _config(*, enabled: bool = True, mode: str = "hot") -> UtilitySimConfig:
    return UtilitySimConfig(
        utility=UtilitySpec("AUSTIN_ENERGY", "AEN", enabled),
        channel="fake",
        channel_settings={},
        schedule=ScheduleConfig(),
        weather=WeatherConfig(mode=mode),  # type: ignore[arg-type]
        tick_s=5.0,
        status_poll_s=30.0,
    )


def _at(hh: int, mm: int) -> float:
    return datetime.combine(DAY, time(hh, mm), tzinfo=LOCAL_TZ).timestamp()


# --- schedule / weather --------------------------------------------------------------------------------


def test_random_weather_is_reproducible_per_date() -> None:
    cfg = WeatherConfig(mode="random", seed=7)
    assert day_weather(DAY, cfg) == day_weather(DAY, cfg)
    days = [day_weather(DAY + timedelta(days=i), cfg) for i in range(60)]
    assert any(d.hot for d in days) and any(not d.hot for d in days)
    assert all(cfg.min_f <= d.high_f <= cfg.max_f for d in days)


def test_a_hot_day_plans_one_discharge_call_inside_the_window() -> None:
    plan = plan_for_day(
        DAY, day_weather(DAY, WeatherConfig(mode="hot")), ScheduleConfig(), utility_id="AUSTIN_ENERGY"
    )
    assert plan is not None and plan.kw == -20_000.0 and plan.duration_min == 60
    local_start = plan.start.astimezone(LOCAL_TZ)
    assert time(16, 30) <= local_start.time() and plan.end.astimezone(LOCAL_TZ).time() <= time(18, 0)
    assert local_start.minute % 5 == 0 and plan.call_ref == "austin_energy-peak-2026-09-28"


def test_a_mild_day_or_disabled_schedule_plans_nothing() -> None:
    mild = day_weather(DAY, WeatherConfig(mode="mild"))
    assert plan_for_day(DAY, mild, ScheduleConfig(), utility_id="A") is None
    hot = day_weather(DAY, WeatherConfig(mode="hot"))
    assert plan_for_day(DAY, hot, ScheduleConfig(enabled=False), utility_id="A") is None


def test_duration_never_exceeds_the_window_or_the_product() -> None:
    cfg = ScheduleConfig(window_start=time(17, 0), window_end=time(17, 30), duration_min=90)
    plan = plan_for_day(DAY, day_weather(DAY, WeatherConfig(mode="hot")), cfg, utility_id="A")
    assert plan is not None and plan.duration_min == 30


# --- config --------------------------------------------------------------------------------------------


def test_the_shipped_config_defaults_to_austin_energy_with_lcra_and_rayburn_disabled(monkeypatch) -> None:
    monkeypatch.delenv("OGSIM_UTILITY", raising=False)
    cfg = load_config(with_mqtt=False)
    assert cfg.utility == UtilitySpec("AUSTIN_ENERGY", "AEN", True)
    assert cfg.channel == "customer_api" and cfg.channel_settings["env_code"] == "AEN"
    assert set(cfg.known_utilities) == {"AUSTIN_ENERGY", "LCRA", "RAYBURN"}
    monkeypatch.setenv("OGSIM_UTILITY", "LCRA")
    assert load_config(with_mqtt=False).utility.enabled is False
    monkeypatch.setenv("OGSIM_UTILITY", "NOPE")
    with pytest.raises(UtilityConfigError):
        load_config(with_mqtt=False)


# --- scenarios -----------------------------------------------------------------------------------------


async def _run(name: str, params: dict[str, Any] | None = None) -> tuple[FakeChannel, Any]:
    channel = FakeChannel()
    clock = FakeClock(_at(12, 0))
    sim = UtilitySim(_config(), channel, clock)
    task = asyncio.create_task(sim.run_scenario(name, params or {"cancel_after_s": 30}))
    while not task.done():
        await asyncio.sleep(0)
        clock.advance(10.0)
    return channel, task.result()


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_every_scenario_passes_against_an_orchestrator_that_applies_the_checks(name) -> None:
    channel, outcome = await _run(name)
    assert outcome.passed, outcome.to_log()
    assert channel.active == {} or name == "utility_call_normal"  # scenarios clean up, normal runs on


async def test_the_charge_scenario_sends_a_positive_kw() -> None:
    channel, _ = await _run("utility_call_charge")
    assert channel.issued[0].kw > 0


def test_every_scenario_is_in_the_catalogue_and_the_scenarios_dir() -> None:
    from pathlib import Path

    import yaml

    for name in SCENARIOS:
        assert catalogue.owner_of(name) == "utility"
    scen_dir = Path(__file__).resolve().parents[1] / "scenarios"
    used = {
        step["type"]
        for p in scen_dir.glob("utility-aen-*.yaml")
        for step in yaml.safe_load(p.read_text(encoding="utf-8"))["steps"]
    }
    assert used == set(SCENARIOS)


# --- runtime -------------------------------------------------------------------------------------------


async def test_the_runtime_issues_the_peak_call_once_and_follows_it() -> None:
    channel = FakeChannel()
    clock = FakeClock(_at(16, 0))
    sim = UtilitySim(_config(), channel, clock)
    await sim.tick()
    assert channel.issued == []  # before the window
    plan = sim.today.plan if sim.today else None
    assert plan is not None
    clock.advance(plan.start.timestamp() - clock.now())
    await sim.tick()
    await sim.tick()
    assert len(channel.issued) == 1 and channel.issued[0].kw == -20_000.0
    clock.advance(31)
    await sim.tick()
    assert sim.today is not None and sim.today.last is not None and sim.today.last.granted_kw == -20_000.0


async def test_the_runtime_skips_a_day_without_a_reservation() -> None:
    channel = FakeChannel(today=False)
    clock = FakeClock(_at(16, 29))
    sim = UtilitySim(_config(), channel, clock)
    await sim.tick()
    clock.advance(sim.today.plan.start.timestamp() - clock.now())  # type: ignore[union-attr]
    await sim.tick()
    assert channel.issued == [] and sim.today.skipped_reason  # type: ignore[union-attr]


async def test_a_transport_failure_is_retried_next_tick() -> None:
    channel = FakeChannel(fail=True)
    clock = FakeClock(_at(16, 29))
    sim = UtilitySim(_config(), channel, clock)
    await sim.tick()
    clock.advance(sim.today.plan.start.timestamp() - clock.now())  # type: ignore[union-attr]
    await sim.tick()
    assert sim.today.issued is None  # type: ignore[union-attr]
    channel.fail = False
    await sim.tick()
    assert len(channel.issued) == 1


async def test_a_disabled_utility_never_calls_and_refuses_scenarios() -> None:
    sim = UtilitySim(_config(enabled=False), None, FakeClock(_at(17, 0)))
    await sim.tick()
    cmd = {
        "id": str(uuid.uuid4()),
        "target": {"kind": "sim", "ref": "AUSTIN_ENERGY"},
        "type": "UTILITY_CALL_NORMAL",
        "params": {},
        "start": datetime.now(UTC).isoformat(),
        "duration_s": 1,
    }
    assert sim.handle_scenario_cmd(cmd) is True and sim.outcomes == []


async def test_a_trigger_for_this_utility_runs_its_scenario() -> None:
    channel = FakeChannel()
    sim = UtilitySim(_config(), channel, FakeClock(_at(12, 0)))

    def cmd(ref: str, wire: str = "UTILITY_CALL_CHARGE") -> dict[str, Any]:
        return {
            "id": str(uuid.uuid4()),
            "target": {"kind": "sim", "ref": ref},
            "type": wire,
            "params": {},
            "start": datetime.now(UTC).isoformat(),
            "duration_s": 1,
        }

    assert sim.handle_scenario_cmd(cmd("CPS_ENERGY")) is False
    assert sim.handle_scenario_cmd(cmd("*", "FLEET_HUB_OFFLINE")) is False
    assert sim.handle_scenario_cmd(cmd("AUSTIN_ENERGY")) is True
    for _ in range(5):
        await asyncio.sleep(0)
    assert [o.name for o in sim.outcomes] == ["utility_call_charge"] and sim.outcomes[0].passed


# --- customer_api channel ------------------------------------------------------------------------------


@dataclass
class FakeTransport:
    answers: list[HttpResult]
    calls: list[tuple[str, str, dict[str, Any] | None]] = field(default_factory=list)

    async def get(self, path: str, auth: tuple[str, str]) -> HttpResult:
        self.calls.append(("GET", path, None))
        return self.answers.pop(0)

    async def post(self, path: str, json: dict[str, Any], auth: tuple[str, str]) -> HttpResult:
        self.calls.append(("POST", path, json))
        return self.answers.pop(0)


def _spec(kw: float = -1000.0) -> CallSpec:
    return CallSpec("aen-1", kw, datetime(2026, 9, 28, 21, 40, tzinfo=UTC), 30)


async def test_customer_api_channel_maps_accept_status_and_cancel() -> None:
    transport = FakeTransport(
        [
            HttpResult(201, {"call_id": "c1", "outcome": "ACCEPTED", "state": "ACTIVE"}),
            HttpResult(
                200,
                {
                    "call_id": "c1",
                    "outcome": "ACCEPTED",
                    "state": "ACTIVE",
                    "granted_kw": -900.0,
                    "granted_kwh": 3.0,
                },
            ),
            HttpResult(200, {"call_id": "c1", "outcome": "ACCEPTED", "state": "COMPLETED"}),
        ]
    )
    channel = CustomerApiChannel(transport, ("u", "p"))
    assert (await channel.issue_call(_spec())).accepted
    assert transport.calls[0][2]["idempotency_key"] == "aen-1"  # type: ignore[index]
    status = await channel.status("aen-1")
    assert status.state == "ACTIVE" and status.granted_kw == -900.0
    assert (await channel.cancel("aen-1")).state == "COMPLETED"
    assert [c[1] for c in transport.calls] == [
        f"{API_PREFIX}/calls",
        f"{API_PREFIX}/calls/c1",
        f"{API_PREFIX}/calls/c1/cancel",
    ]


async def test_customer_api_channel_maps_a_refusal_and_raises_on_transport_failure() -> None:
    refused = {"detail": {"reason_code": "R-CALL-CHARGE-REFUSED", "detail": "x", "call_id": "c2"}}
    channel = CustomerApiChannel(FakeTransport([HttpResult(422, refused), HttpResult(503, {})]), ("u", "p"))
    result = await channel.issue_call(_spec(kw=1000.0))
    assert (result.accepted, result.state, result.reason_code) == (False, "REFUSED", "R-CALL-CHARGE-REFUSED")
    with pytest.raises(ChannelError):
        await channel.issue_call(_spec())
    assert (await channel.status("never-issued")).state == "UNKNOWN"


def test_channel_registry_refuses_an_unknown_channel_and_needs_credentials(monkeypatch) -> None:
    assert "customer_api" in CHANNEL_FACTORIES
    with pytest.raises(ChannelError):
        build_channel("nope", {})
    monkeypatch.delenv("OGSIM_UTILITY_AEN_USER", raising=False)
    with pytest.raises(ChannelError):
        build_channel("customer_api", {"base_url": "https://example.test", "env_code": "AEN"})
