"""Grid-link service (grid-link.md S5): validation and allow-list, toll calls through the core port, the
fail-safe heartbeat watchdog, L2 folding and delivery, tracing with origin GRID_LINK."""

from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path
from typing import Any

import pytest

from opengrid.core.models.mqtt import ScadaUtilityInstruction
from opengrid.integrations.grid_link.config import OVERRIDE_ENV, grid_link_table, load_grid_link_settings
from opengrid.integrations.grid_link.model import (
    CallOutcome,
    CallPhase,
    CancelCall,
    ControlVerdict,
    Heartbeat,
    L2Block,
    L2Limit,
    L2LimitValue,
    TollCall,
    reason_number,
)
from opengrid.integrations.grid_link.service import DENY_TRACE_MIN_INTERVAL_S, ORIGIN

from .fakes import FakeCalls, Harness, make_harness, make_settings

PEER = "127.0.0.1"
SHIPPED_CONFIG = Path(__file__).resolve().parents[4] / "config" / "orchestrator.toml"


def _alive(h: Harness) -> None:
    assert h.service.offer(Heartbeat(), PEER) is ControlVerdict.ACCEPTED


async def test_toll_call_refused_until_the_first_heartbeat_fail_safe(harness: Harness) -> None:
    verdict = harness.service.offer(TollCall(7, 1000.0, 60), PEER)
    assert verdict is ControlVerdict.INHIBITED
    await harness.service.drain()
    assert harness.calls.issued == []
    denies = harness.trace.events("GRID_LINK_DENY")
    assert denies and denies[0]["origin"] == ORIGIN and denies[0]["reason"] == "control-inhibited"


async def test_toll_call_goes_through_the_core_port_and_is_traced_with_origin_grid_link(
    harness: Harness,
) -> None:
    _alive(harness)
    assert harness.service.offer(TollCall(7, 1500.0, 90), PEER) is ControlVerdict.ACCEPTED
    await harness.service.drain()
    assert harness.calls.issued == [("AUSTIN_ENERGY", 7, 1500.0, 90)]
    traced = harness.trace.events("GRID_LINK_COMMAND")
    assert traced == [
        {
            "origin": "GRID_LINK",
            "utility_id": "AUSTIN_ENERGY",
            "peer": PEER,
            "command": "TollCall",
            "ems_call_id": 7,
            "setpoint_kw": 1500.0,
            "duration_min": 90,
        }
    ]
    status = harness.service.status()
    assert status.call_phase is CallPhase.ACCEPTED and status.ems_call_id == 7 and status.call_active


async def test_core_refusal_is_reported_with_its_reason_code(harness: Harness) -> None:
    _alive(harness)
    harness.service.offer(TollCall(8, 9000.0, 30), PEER)
    await harness.service.drain()
    status = harness.service.status()
    assert status.call_phase is CallPhase.REJECTED
    assert status.call_reason == reason_number("R-CALL-OVER-COMMITTED") == 8
    assert status.call_rejected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (TollCall(1, 0.0, 30), ControlVerdict.OUT_OF_RANGE),
        (TollCall(1, 26001.0, 30), ControlVerdict.OUT_OF_RANGE),
        (TollCall(1, 100.0, 91), ControlVerdict.OUT_OF_RANGE),  # D-29: never more than 90 min
        (TollCall(0, 100.0, 30), ControlVerdict.OUT_OF_RANGE),
        (L2Limit("NOPE", True), ControlVerdict.NOT_SUPPORTED),
        (L2LimitValue("B041", -1.0), ControlVerdict.OUT_OF_RANGE),
        (L2Block("B041", True), ControlVerdict.ACCEPTED),
    ],
)
def test_ts_gl_validation_limits_and_allow_list(
    harness: Harness, command: Any, expected: ControlVerdict
) -> None:
    _alive(harness)
    assert harness.service.validate(command) is expected


def test_utility_switches_refuse_as_not_authorized() -> None:
    h = make_harness(make_settings(accept_toll_calls=False, accept_l2=False))
    _alive(h)
    assert h.service.validate(TollCall(1, 10.0, 10)) is ControlVerdict.NOT_AUTHORIZED
    assert h.service.validate(CancelCall(None)) is ControlVerdict.NOT_AUTHORIZED
    assert h.service.validate(L2Block("B041", True)) is ControlVerdict.NOT_AUTHORIZED


async def test_heartbeat_loss_refuses_new_calls_keeps_l2_and_allows_cancel(harness: Harness) -> None:
    _alive(harness)
    harness.service.offer(L2LimitValue("B041", 250.0), PEER)
    harness.service.offer(L2Limit("B041", True), PEER)
    harness.service.offer(TollCall(9, 1000.0, 60), PEER)
    await harness.service.drain()
    await harness.service.tick()
    before = len(harness.sink.instructions)
    assert harness.trace.events("GRID_LINK_STATE")[-1]["state"] == "HEALTHY"

    harness.clock.advance(31.0)
    await harness.service.tick()

    assert harness.trace.events("GRID_LINK_STATE")[-1]["state"] == "HEARTBEAT_LOST"
    assert harness.service.offer(TollCall(10, 500.0, 30), PEER) is ControlVerdict.INHIBITED
    assert harness.service.offer(CancelCall(9), PEER) is ControlVerdict.ACCEPTED
    await harness.service.drain()
    assert harness.calls.cancelled == [("AUSTIN_ENERGY", 9)]
    # no lift was emitted: the L2 limit stays as last received
    assert len(harness.sink.instructions) == before
    assert harness.service.l2.ceiling_kw("bank-041") == 250.0
    status = harness.service.status()
    assert not status.link_healthy and status.l2_active


async def test_l2_zone_and_bank_targets_fold_to_the_most_restrictive(harness: Harness) -> None:
    _alive(harness)
    for command in (
        L2LimitValue("LZ_AEN", 400.0),
        L2Limit("LZ_AEN", True),
        L2LimitValue("B041", 150.0),
        L2Limit("B041", True),
    ):
        harness.service.offer(command, PEER)
    await harness.service.drain()
    latest = _latest(harness.sink.instructions)
    assert latest["bank-040"].kind == "LIMIT" and latest["bank-040"].limit_kw == 400.0
    assert latest["bank-041"].kind == "LIMIT" and latest["bank-041"].limit_kw == 150.0
    assert all(i.issued_by == "UTILITY_GRID_LINK" and i.expires_at is None for i in latest.values())

    harness.service.offer(L2Block("LZ_AEN", True), PEER)
    await harness.service.drain()
    latest = _latest(harness.sink.instructions)
    assert latest["bank-040"].kind == "BLOCK" and latest["bank-041"].kind == "BLOCK"

    harness.service.offer(L2Block("LZ_AEN", False), PEER)
    harness.service.offer(L2Limit("LZ_AEN", False), PEER)
    harness.service.offer(L2Limit("B041", False), PEER)
    await harness.service.drain()
    lifted = _latest(harness.sink.instructions)
    assert all(i.expires_at == i.issued_at for i in lifted.values())  # the tracker's lift convention


async def test_limit_switched_on_without_a_value_is_zero_kw_fail_closed(harness: Harness) -> None:
    _alive(harness)
    harness.service.offer(L2Limit("B041", True), PEER)
    await harness.service.drain()
    instruction = _latest(harness.sink.instructions)["bank-041"]
    assert instruction.kind == "LIMIT" and instruction.limit_kw == 0.0


async def test_l2_delivery_failure_is_retried_on_the_next_tick() -> None:
    class FlakySink:
        def __init__(self) -> None:
            self.fail = True
            self.instructions: list[ScadaUtilityInstruction] = []

        async def on_bank_signal(self, signal: Any) -> None:
            return None

        async def on_utility_instruction(self, instruction: ScadaUtilityInstruction) -> None:
            if self.fail:
                raise ConnectionError("broker down")
            self.instructions.append(instruction)

    sink = FlakySink()
    h = make_harness(sink=sink)
    _alive(h)
    h.service.offer(L2Block("B041", True), PEER)
    await h.service.drain()
    assert sink.instructions == []
    sink.fail = False
    await h.service.tick()
    assert [i.kind for i in sink.instructions] == ["BLOCK"]


async def test_status_aggregates_telemetry_and_reports_l2_ceilings(harness: Harness) -> None:
    _alive(harness)
    harness.service.offer(L2Block("B041", True), PEER)
    await harness.service.drain()
    await harness.service.tick()
    status = harness.service.status()
    assert status.available_kw == 3000.0 and status.soc_pct == 50.0 and not status.telemetry_stale
    ceilings = {b.bank_id: b.l2_ceiling_kw for b in status.banks}
    assert ceilings == {"bank-040": None, "bank-041": 0.0, "bank-042": None}


async def test_core_timeout_is_reported_then_corrected_by_status_refresh() -> None:
    class SlowCalls(FakeCalls):
        async def issue(
            self, utility_id: str, ems_call_id: int, setpoint_kw: float, duration_min: int
        ) -> CallOutcome:
            await asyncio.sleep(10)
            raise AssertionError("unreachable: the service times out first")

    h = make_harness(make_settings(core_timeout_s=0.05))
    slow = SlowCalls()
    h.service._calls = slow
    _alive(h)
    h.service.offer(TollCall(11, 100.0, 30), PEER)
    await h.service.drain()
    assert h.service.status().call_reason == reason_number("R-GL-CORE-TIMEOUT")
    slow.calls[11] = CallOutcome(CallPhase.ACTIVE, delivered_kw=90.0)  # the core had created it after all
    await h.service.tick()
    status = h.service.status()
    assert status.call_phase is CallPhase.ACTIVE and status.call_delivered_kw == 90.0


def test_denies_are_rate_limited_per_peer_and_reason(harness: Harness) -> None:
    for _ in range(5):
        harness.service.deny("10.9.9.9", "peer-not-allowed", "association refused")
    assert harness.service._queue.qsize() == 1
    harness.clock.advance(DENY_TRACE_MIN_INTERVAL_S + 0.1)
    harness.service.deny("10.9.9.9", "peer-not-allowed", "association refused")
    assert harness.service._queue.qsize() == 2


def test_config_is_disabled_by_default_and_fails_closed_on_insecure_listeners() -> None:
    assert load_grid_link_settings(None).active_utilities() == []
    raw = {"utility_id": "AUSTIN_ENERGY", "listen_port": 20001, "allowed_peers": ["10.0.0.1"], "banks": ["b"]}
    with pytest.raises(ValueError, match="requires TLS"):
        load_grid_link_settings(
            {"enabled": True, "utilities": [{**raw, "listen_host": "0.0.0.0", "max_setpoint_kw": 1}]}  # noqa: S104 -- asserts it is refused
        )
    with pytest.raises(ValueError, match="allowed_peer_cns"):
        load_grid_link_settings({"utilities": [{**raw, "max_setpoint_kw": 1, "tls": {"enabled": True}}]})
    with pytest.raises(ValueError):
        load_grid_link_settings({"utilities": [{**raw, "max_setpoint_kw": 1, "max_duration_min": 120}]})
    settings = load_grid_link_settings({"enabled": True, "utilities": [{**raw, "max_setpoint_kw": 1}]})
    assert settings.active_utilities() == []  # the utility's own switch is still off


def _latest(instructions: list[ScadaUtilityInstruction]) -> dict[str, ScadaUtilityInstruction]:
    out: dict[str, ScadaUtilityInstruction] = {}
    for instruction in instructions:
        out[instruction.bank_id] = instruction
    return out


def test_shipped_config_is_disabled_and_every_utility_entry_validates() -> None:
    raw = tomllib.loads(SHIPPED_CONFIG.read_text(encoding="utf-8"))["grid_link"]
    settings = load_grid_link_settings(raw)
    assert settings.enabled is False and settings.active_utilities() == []
    assert {u.utility_id for u in settings.utilities} >= {"AUSTIN_ENERGY", "LCRA", "RAYBURN"}
    assert all(u.enabled is False and u.tls.enabled for u in settings.utilities)


def test_unknown_utility_ids_are_skipped_not_fatal() -> None:
    raw = {
        "utility_id": "X",
        "listen_port": 1,
        "allowed_peers": ["127.0.0.1"],
        "banks": ["b"],
        "max_setpoint_kw": 1,
    }
    good = {**raw, "utility_id": "AUSTIN_ENERGY", "listen_port": 2, "enabled": True}
    settings = load_grid_link_settings({"enabled": True, "utilities": [{**raw, "enabled": True}, good]})
    assert [u.utility_id for u in settings.active_utilities()] == ["AUSTIN_ENERGY"]
    assert settings.unknown_utilities() == ["X"]


def test_host_override_file_replaces_the_release_grid_link_table(tmp_path: Path, monkeypatch: Any) -> None:
    release = {"enabled": False}
    monkeypatch.delenv(OVERRIDE_ENV, raising=False)
    assert grid_link_table(release) is release
    override = tmp_path / "grid_link.toml"
    override.write_text("[grid_link]\nenabled = true\n", encoding="utf-8")
    monkeypatch.setenv(OVERRIDE_ENV, str(override))
    assert grid_link_table(release) == {"enabled": True}
    override.write_text("[other]\nx = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no \\[grid_link\\]"):
        grid_link_table(release)
    monkeypatch.setenv(OVERRIDE_ENV, str(tmp_path / "missing.toml"))
    with pytest.raises(OSError):
        grid_link_table(release)
