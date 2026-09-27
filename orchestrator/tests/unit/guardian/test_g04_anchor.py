"""G-04 utility-scale anchor (r3.4.0.1 toll-ramp fix): signed setpoint whenever its lease is live."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from opengrid.guardian import checks

NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
CYCLE = 2.0


def _anchor(**over: object) -> checks.G04Anchor:
    kwargs: dict[str, object] = {
        "prev_telemetry_kw": 0.0,
        "telemetry_ts": NOW - timedelta(seconds=10),
        "last_signed_kw": -444.0,
        "last_signed_at": NOW - timedelta(seconds=CYCLE),
        "lease_expires_at": NOW + timedelta(seconds=4),
        "now": NOW,
        "utility_scale": True,
        "cycle_interval_s": CYCLE,
    }
    kwargs.update(over)
    return checks.g04_anchor_kw(**kwargs)  # type: ignore[arg-type]


def test_utility_scale_anchors_at_signed_setpoint_while_lease_live() -> None:
    a = _anchor()
    assert (a.kw, a.dt_s, a.source) == (-444.0, CYCLE, "SIGNED")


def test_tie_with_telemetry_timestamp_still_anchors_signed() -> None:
    # lead's rule: whenever the lease is live -- a telemetry sample stamped at or after the signing time does not
    # drop the anchor back to stale telemetry
    a = _anchor(telemetry_ts=NOW - timedelta(seconds=CYCLE))
    assert a.source == "SIGNED"
    a = _anchor(telemetry_ts=NOW)
    assert a.source == "SIGNED"


def test_expired_lease_falls_back_to_telemetry() -> None:
    a = _anchor(lease_expires_at=NOW)
    assert (a.kw, a.dt_s, a.source) == (0.0, CYCLE, "TELEMETRY")


def test_homes_always_use_telemetry() -> None:
    assert _anchor(utility_scale=False).source == "TELEMETRY"


def test_no_signed_setpoint_uses_telemetry() -> None:
    assert _anchor(last_signed_kw=None, last_signed_at=None, lease_expires_at=None).source == "TELEMETRY"


def test_an_older_signature_never_widens_dt() -> None:
    """r3.4.3 HIGH-B: the signed anchor is only the starting point; the bound is always one cycle."""
    for age_s in (3 * CYCLE, 20.0, 28.0):
        a = _anchor(
            last_signed_at=NOW - timedelta(seconds=age_s), lease_expires_at=NOW + timedelta(seconds=2)
        )
        assert (a.source, a.dt_s) == ("SIGNED", CYCLE)


def test_the_rate_checks_count_the_full_step_from_the_anchor() -> None:
    assert checks.ramp_step_kw(-800.0, checks.G04Anchor(-400.0, CYCLE, "SIGNED")) == -400.0
    assert checks.ramp_step_kw(-222.0, checks.G04Anchor(0.0, CYCLE, "TELEMETRY")) == -222.0


def test_20mw_toll_ramp_passes_g04_every_cycle_and_overshoot_is_vetoed() -> None:
    """10 cycles of a 20 MW toll at the full hub rate: stale telemetry (0 kW) would veto from cycle 2; the signed
    anchor passes each step, and a 1.5x step is still vetoed."""
    rate_kw_per_s = 20_000.0 / 180.0
    step = rate_kw_per_s * CYCLE
    signed_kw: float | None = None
    signed_at: datetime | None = None
    for n in range(1, 11):
        now = NOW + timedelta(seconds=n * CYCLE)
        anchor = checks.g04_anchor_kw(
            prev_telemetry_kw=0.0,
            telemetry_ts=NOW,
            last_signed_kw=signed_kw,
            last_signed_at=signed_at,
            lease_expires_at=(signed_at + timedelta(seconds=6)) if signed_at else None,
            now=now,
            utility_scale=True,
            cycle_interval_s=CYCLE,
        )
        target = -step * n
        ok = abs(target - anchor.kw) <= rate_kw_per_s * anchor.dt_s + 1e-6
        assert ok, n
        over = anchor.kw - 1.5 * step
        assert abs(over - anchor.kw) > rate_kw_per_s * anchor.dt_s + 1e-6
        signed_kw, signed_at = target, now
