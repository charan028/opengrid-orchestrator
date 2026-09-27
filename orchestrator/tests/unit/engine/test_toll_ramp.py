"""Toll ramp (D-29 20 MW set) end to end: the engine's utility-scale setpoints, checked by the guardian's G-04
with its anchor rule (`guardian.checks.g04_anchor_kw`, SAFETY: the last SIGNED setpoint within its lease for
utility-scale hubs, else telemetry). Live finding r3.4: stepping from the engine's own command while G-04
anchored at 10 s-old telemetry vetoed every batch and the call never delivered.

Skipped until SAFETY's `g04_anchor_kw` is on the branch under test (integ/fix-safety-r341)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from opengrid import engine
from opengrid.core.physics import HubParams, hub_ramp_kw_per_s
from opengrid.guardian import checks
from opengrid.guardian.ports import ProposedItem

pytestmark = pytest.mark.skipif(
    not hasattr(checks, "g04_anchor_kw"), reason="needs SAFETY's guardian.checks.g04_anchor_kw"
)

CYCLE_S = 2.0
TELEMETRY_EVERY = 5  # cycles (10 s)
T0 = datetime(2026, 9, 27, 16, 30, tzinfo=UTC)
PARAMS = HubParams(e_kwh=40_000.0, r_kwh=8_000.0, p_kw=20_000.0, utility_scale=True)
RAMP = hub_ramp_kw_per_s(PARAMS)


@dataclass
class _Asset:
    """The engine's view of the substation set: telemetry `p_kw` lags (updated every 10 s)."""

    hub_id: str = "sub-aen-01"
    bank_id: str = "bank-aen-sub"
    p_kw: float = 0.0
    ramp_kw_per_s: float = RAMP
    utility_scale: bool = True


def _item(setpoint: float) -> ProposedItem:
    return ProposedItem(hub_id="sub-aen-01", p_kw_setpoint=setpoint, reason_code="R-GRANT-COMMITTED")


def _run(cycles: int, target_kw: float) -> tuple[list[float], int]:
    engine._last_commanded_kw.pop("sub-aen-01", None)
    asset = _Asset()
    telemetry_kw, telemetry_ts = 0.0, T0
    signed_kw: float | None = None
    signed_at: datetime | None = None
    setpoints: list[float] = []
    vetoes = 0
    for k in range(cycles):
        now = T0 + timedelta(seconds=CYCLE_S * (k + 1))
        if k % TELEMETRY_EVERY == 0 and signed_kw is not None:
            # The asset reports what it was last told, sampled just before this cycle.
            telemetry_kw, telemetry_ts = signed_kw, now - timedelta(seconds=0.5)
            asset.p_kw = telemetry_kw
        setpoint = engine._ramped_setpoint_kw(asset, target_kw, CYCLE_S)
        anchor = checks.g04_anchor_kw(
            prev_telemetry_kw=telemetry_kw,
            telemetry_ts=telemetry_ts,
            last_signed_kw=signed_kw,
            last_signed_at=signed_at,
            lease_expires_at=(signed_at + timedelta(seconds=30)) if signed_at else None,
            now=now,
            utility_scale=True,
            cycle_interval_s=CYCLE_S,
        )
        outcome = checks.check_g04_hub_ramp(_item(setpoint), anchor.kw, anchor.dt_s, RAMP)
        if outcome.ok:
            signed_kw, signed_at = setpoint, now
        else:
            vetoes += 1
        setpoints.append(setpoint)
    return setpoints, vetoes


def test_ten_cycles_between_telemetry_updates_get_no_g04_veto() -> None:
    setpoints, vetoes = _run(10, -20_000.0)
    assert vetoes == 0
    assert setpoints == sorted(setpoints, reverse=True)  # monotonic toward the discharge target


def test_a_20_mw_call_reaches_full_power_at_the_g04_rate_without_vetoes() -> None:
    cycles = int(20_000.0 / (RAMP * engine.RAMP_SAFETY_FACTOR * CYCLE_S)) + 3
    setpoints, vetoes = _run(cycles, -20_000.0)
    assert vetoes == 0
    assert setpoints[-1] == pytest.approx(-20_000.0)
    assert cycles * CYCLE_S < 4 * 60  # full power in minutes, not the hour telemetry-stepping took


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
    over = -400.0 - 1.5 * RAMP * anchor.dt_s
    assert not checks.check_g04_hub_ramp(_item(over), anchor.kw, anchor.dt_s, RAMP).ok
    within = -400.0 - 0.9 * RAMP * anchor.dt_s
    assert checks.check_g04_hub_ramp(_item(within), anchor.kw, anchor.dt_s, RAMP).ok
