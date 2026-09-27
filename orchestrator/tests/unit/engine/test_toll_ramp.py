"""Toll ramp (D-29 20 MW set) end to end: the engine's utility-scale setpoints (`engine.ramp_anchor`), checked by
the guardian's G-04 with its anchor rule (`guardian.checks.g04_anchor_kw`: the last SIGNED setpoint within its
lease for utility-scale hubs, else telemetry).

HIGH-A (r3.4.2, workstation probe with the real code): the engine stepped from its last PROPOSAL, refreshed on
every proposal whether signed or not, so after a signing gap of 30 s or more it walked to -20 MW from 0 and
G-04 vetoed every cycle for the rest of the call (80/80; 275/295 after a guardian restart). The simulation below
replays the probe's scenarios -- a 40 s, 28 s and 20 s safe-stop hold, and a guardian restart with a 10 s
gap -- and each must converge to the target."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from opengrid import engine
from opengrid.core.physics import HubParams, hub_ramp_kw_per_s
from opengrid.engine.ramp_anchor import RampAnchors, stopped_banks
from opengrid.guardian import checks
from opengrid.guardian.ports import ProposedItem

CYCLE_S = 2.0
LEASE_S = 30.0
TELEMETRY_EVERY_S = 10.0
T0 = datetime(2026, 9, 27, 16, 30, tzinfo=UTC)
PARAMS = HubParams(e_kwh=40_000.0, r_kwh=8_000.0, p_kw=20_000.0, utility_scale=True)
RAMP = hub_ramp_kw_per_s(PARAMS)
HUB, BANK = "sub-aen-01", "bank-aen-sub"
TARGET = -20_000.0
FULL_RAMP_CYCLES = int(abs(TARGET) / (RAMP * engine.RAMP_SAFETY_FACTOR * CYCLE_S)) + 2


@pytest.fixture(autouse=True)
def _fresh_anchors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "_ramp_anchors", RampAnchors())


@dataclass
class _AssetView:
    """The engine's twin view of the set: telemetry `p_kw` sampled every 10 s."""

    hub_id: str = HUB
    bank_id: str = BANK
    p_kw: float = 0.0
    last_seen_at: datetime | None = None
    ramp_kw_per_s: float = RAMP
    utility_scale: bool = True


@dataclass
class _Guardian:
    """G-04 with the guardian's own in-memory signed record (lost on a restart)."""

    signed: tuple[float, datetime, datetime] | None = None
    clears_on_stop: bool = True
    one_cycle_dt: bool = False

    def check(
        self, setpoint: float, telemetry_kw: float, telemetry_ts: datetime | None, now: datetime
    ) -> bool:
        anchor = checks.g04_anchor_kw(
            prev_telemetry_kw=telemetry_kw,
            telemetry_ts=telemetry_ts,
            last_signed_kw=self.signed[0] if self.signed else None,
            last_signed_at=self.signed[1] if self.signed else None,
            lease_expires_at=self.signed[2] if self.signed else None,
            now=now,
            utility_scale=True,
            cycle_interval_s=CYCLE_S,
        )
        dt = CYCLE_S if self.one_cycle_dt else anchor.dt_s
        item = ProposedItem(hub_id=HUB, p_kw_setpoint=setpoint, reason_code="R-GRANT-COMMITTED")
        ok = checks.check_g04_hub_ramp(item, anchor.kw, dt, RAMP).ok
        if ok:
            self.signed = (setpoint, now, now + timedelta(seconds=LEASE_S))
        return ok


@dataclass
class Result:
    setpoints: list[float]
    vetoes: int
    vetoes_after_gap: int
    final_power_kw: float


def _simulate(
    *,
    cycles: int,
    stop_at_s: float | None = None,
    stop_for_s: float = 0.0,
    guardian_down_at_s: float | None = None,
    guardian_down_for_s: float = 0.0,
    clears_on_stop: bool = True,
    one_cycle_dt: bool = False,
) -> Result:
    anchors = RampAnchors()
    engine._ramp_anchors = anchors  # fresh engine state for this run
    guardian = _Guardian(clears_on_stop=clears_on_stop, one_cycle_dt=one_cycle_dt)
    view = _AssetView()
    signed_power: tuple[float, datetime] | None = None  # what the asset follows: (kW, lease end)
    telemetry_kw, telemetry_ts = 0.0, None
    next_sample = T0
    stopped_before = False
    pending_outcome: dict = {}
    setpoints: list[float] = []
    vetoes = vetoes_after_gap = 0
    gap_end = None
    for k in range(cycles):
        now = T0 + timedelta(seconds=CYCLE_S * k)
        t = CYCLE_S * k
        stopped = stop_at_s is not None and stop_at_s <= t < stop_at_s + stop_for_s
        guardian_down = (
            guardian_down_at_s is not None
            and guardian_down_at_s <= t < guardian_down_at_s + guardian_down_for_s
        )
        if stop_at_s is not None and t >= stop_at_s + stop_for_s:
            gap_end = gap_end or now
        if guardian_down_at_s is not None and t >= guardian_down_at_s + guardian_down_for_s:
            gap_end = gap_end or now
        if guardian_down:
            guardian.signed = None  # a restart loses the in-memory signed record

        # The asset: follows its signed setpoint while the lease is live; 0 on a stop or a lapsed lease.
        if stopped or signed_power is None or signed_power[1] <= now:
            actual_kw = 0.0
            if stopped:
                signed_power = None
        else:
            actual_kw = signed_power[0]
        if now >= next_sample:
            telemetry_kw, telemetry_ts = actual_kw, now - timedelta(seconds=0.5)
            next_sample = now + timedelta(seconds=TELEMETRY_EVERY_S)
        view.p_kw, view.last_seen_at = telemetry_kw, telemetry_ts

        # Engine: stops, then last cycle's verdicts, then this cycle's step.
        if stopped:
            scopes = {("BANK", BANK): ("ENGAGE", T0 + timedelta(seconds=stop_at_s or 0))}
        elif stop_at_s is not None and t >= stop_at_s + stop_for_s:
            scopes = {("BANK", BANK): ("RELEASE", T0 + timedelta(seconds=stop_at_s + stop_for_s))}
        else:
            scopes = {}
        if stopped and not stopped_before and clears_on_stop:
            guardian.signed = None
        stopped_before = stopped
        anchors.apply_stops(scopes, zone_of_bank=lambda b: None)
        anchors.apply_verdicts(pending_outcome, now)
        pending_outcome = {}
        if stopped_banks([BANK], scopes, zone_of_bank=lambda b: None) or guardian_down:
            continue  # no proposals for a stopped bank; guardian unavailable: hold
        setpoint = engine._ramped_setpoint_kw(view, TARGET, CYCLE_S, now=now)
        batch_id = uuid4()
        anchors.record_proposal(
            batch_id, [{"hub_id": HUB, "p_kw_setpoint": setpoint}], now + timedelta(seconds=LEASE_S)
        )
        ok = guardian.check(setpoint, telemetry_kw, telemetry_ts, now)
        pending_outcome[batch_id] = "PASS" if ok else "VETOED"
        if ok:
            signed_power = (setpoint, now + timedelta(seconds=LEASE_S))
        else:
            vetoes += 1
            if gap_end is not None:
                vetoes_after_gap += 1
        setpoints.append(setpoint)
    final = signed_power[0] if signed_power and signed_power[1] > now else 0.0
    return Result(setpoints, vetoes, vetoes_after_gap, final)


def _gap_start_s() -> float:
    return CYCLE_S * (FULL_RAMP_CYCLES // 2)  # mid-ramp, around -10 MW


def test_an_uninterrupted_20_mw_call_reaches_full_power_at_the_g04_rate_without_vetoes() -> None:
    result = _simulate(cycles=FULL_RAMP_CYCLES + 3)
    assert result.vetoes == 0
    assert result.setpoints == sorted(result.setpoints, reverse=True)
    assert result.final_power_kw == pytest.approx(TARGET), result.vetoes
    assert FULL_RAMP_CYCLES * CYCLE_S < 8 * 60


@pytest.mark.parametrize("hold_s", [40.0, 28.0, 20.0])
@pytest.mark.parametrize("clears_on_stop", [True, False])
def test_a_safe_stop_hold_then_release_converges_to_the_target(hold_s: float, clears_on_stop: bool) -> None:
    gap_cycles = int(hold_s / CYCLE_S)
    result = _simulate(
        cycles=2 * FULL_RAMP_CYCLES + gap_cycles + 30,  # the stop drops the set to 0: a full ramp again
        stop_at_s=_gap_start_s(),
        stop_for_s=hold_s,
        clears_on_stop=clears_on_stop,
    )
    assert result.final_power_kw == pytest.approx(TARGET), result.vetoes
    # After the release the hub ramps from 0 again: at most the lease's worth of vetoes while a guardian that
    # kept its pre-stop signature waits for it to lapse, none when it clears it on the stop (the contract).
    assert result.vetoes_after_gap <= (0 if clears_on_stop else int(LEASE_S / CYCLE_S))


def test_a_guardian_restart_with_a_10_s_gap_converges_to_the_target() -> None:
    result = _simulate(
        cycles=FULL_RAMP_CYCLES + 40, guardian_down_at_s=_gap_start_s(), guardian_down_for_s=10.0
    )
    assert result.final_power_kw == pytest.approx(TARGET), result.vetoes
    assert result.vetoes_after_gap <= 2  # one G-04 veto re-anchors the engine to telemetry


@pytest.mark.parametrize("gap_s", [40.0, 10.0])
def test_a_guardian_outage_longer_or_shorter_than_the_lease_converges(gap_s: float) -> None:
    result = _simulate(
        cycles=2 * FULL_RAMP_CYCLES + int(gap_s / CYCLE_S) + 40,  # a lapsed lease drops the set to 0
        guardian_down_at_s=_gap_start_s(),
        guardian_down_for_s=gap_s,
    )
    assert result.final_power_kw == pytest.approx(TARGET), result.vetoes


def test_the_engine_step_also_passes_a_guardian_that_uses_one_cycle_as_dt() -> None:
    result = _simulate(cycles=FULL_RAMP_CYCLES + 3, one_cycle_dt=True)
    assert result.vetoes == 0
    assert result.final_power_kw == pytest.approx(TARGET), result.vetoes


def test_a_real_over_step_is_still_vetoed() -> None:
    anchor = checks.g04_anchor_kw(
        prev_telemetry_kw=0.0,
        telemetry_ts=T0,
        last_signed_kw=-400.0,
        last_signed_at=T0 + timedelta(seconds=2),
        lease_expires_at=T0 + timedelta(seconds=32),
        now=T0 + timedelta(seconds=4),
        utility_scale=True,
        cycle_interval_s=CYCLE_S,
    )
    item = ProposedItem(hub_id=HUB, p_kw_setpoint=-400.0 - 1.5 * RAMP * anchor.dt_s, reason_code="R")
    assert not checks.check_g04_hub_ramp(item, anchor.kw, anchor.dt_s, RAMP).ok
