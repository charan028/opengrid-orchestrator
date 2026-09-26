"""DATA_CENTER closed-loop controller: firm bridging capacity held against the site meter
(06-service-profiles-and-power-quality.md S4.b; `config/service_profiles/data_center.toml`; K9, K13, K14).

**Control law, proportional only.** Each cycle, with a usable site-meter reading (GOOD, no older than the
profile's `feedback_freshness_s`):

    e_k = P_meter_k - P_target_k                     (kW; the meter's p_kw is + import, - export)
    u_k = clamp(u_ff_k + Kp * deadband(e_k, db), 0, min(committed_kw, available_kw))

then rate-limited by the profile's `ramp_limit` (kW/min) through `opengrid.core.physics.apply_ramp_limit`.
`u_ff` is the feed-forward: the obligation's scheduled delivery this cycle (normally its committed kW).
When the site imports more than its target, the fleet delivers more, and less when it imports less.

S4.b writes the fast loop as `u_k = u_{k-1} + Kp e_k`. That form accumulates error, which is integral
action, and S4.b and K9 both require no integrator here: the site's own UPS/ATS transfer logic is the
integrating authority during a bridge and the fleet is feed-forward to it. So the output is positional,
a memoryless function of this cycle's error and feed-forward. The only carried state is the previous
output for the rate limit and the signal-loss clock, and neither accumulates error. The price is a
steady-state error of `e/(1+Kp)`, which the site's integrator removes. `Kp` must be in (0, 1): with the
meter showing the previous command, the loop pole is at -Kp.

**Signal loss** (`failure_behaviour = HOLD_THEN_SCHEDULE`; 03 S8.6.3): no reading, a stale one or a
non-GOOD one holds the delivered level for `hold_s` (T_hold, 15 min default), then follows the schedule,
rate-limited, never a step to standby (K7). Recovery goes straight back to tracking.

**Envelope.** The output never exceeds the commitment (K13) or the deliverable capacity (K4); the guardian
still checks everything. Unused commitment is held idle, never given to another obligation.

**PQ at the point of common coupling** (`pcc_measurement` + `closed_loop_common.monitor_pq`): the site
meter's imbalance, voltage deviation, frequency deviation, THD and current against the customer's
envelope, fed to `pq_monitor`'s S5.4 ladder.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from opengrid.allocator.closed_loop_common import ControlMode, clamp, envelope_ceiling_kw
from opengrid.core.physics import apply_ramp_limit
from opengrid.core.pq import PqMeasurement, phase_unbalance_pct
from opengrid.core.pq.constants import NOMINAL_FREQ_HZ
from opengrid.site_ingest.models import FeedbackValue, SiteMeterReading

#: Proportional gain (kW of fleet output per kW of site-meter error). With one cycle of meter delay the
#: deviation from the loop's fixed point shrinks by a factor Kp per cycle: 0.5 is within 2 % after 6
#: cycles (12 s at the 2 s cadence). S4.b's <= 2 s response to a declared bridge comes from the
#: feed-forward, not from this gain.
DEFAULT_KP = 0.5
#: Kp must stay below 1 (loop pole at -Kp with one cycle of meter delay).
MAX_KP = 1.0
#: T_hold of HOLD_THEN_SCHEDULE (03 S8.6.3: "hold the delivered level for T_hold (15 min default)").
DEFAULT_HOLD_S = 900.0
_SECONDS_PER_MINUTE = 60.0

R_DC_TRACKING = "R-DC-SITE-METER-TRACKING"
R_DC_HOLD = "R-DC-SIGNAL-LOSS-HOLD"
R_DC_SCHEDULE = "R-DC-SIGNAL-LOSS-SCHEDULE"
R_DC_NO_TARGET = "R-DC-NO-TARGET-SCHEDULE"


@dataclass(frozen=True, slots=True)
class DataCenterParams:
    """Per-obligation controller parameters, from the profile and the contract."""

    deadband_kw: float
    ramp_kw_per_min: float
    freshness_s: float
    kp: float = DEFAULT_KP
    hold_s: float = DEFAULT_HOLD_S

    def __post_init__(self) -> None:
        if not 0.0 < self.kp < MAX_KP:
            raise ValueError(f"kp must be in (0, {MAX_KP}), got {self.kp}")
        if self.deadband_kw < 0 or self.ramp_kw_per_min <= 0 or self.freshness_s <= 0 or self.hold_s < 0:
            raise ValueError("deadband/hold must be >= 0, ramp and freshness > 0")


def params_from_profile(
    profile: Mapping[str, Any], committed_kw: float, *, kp: float = DEFAULT_KP
) -> DataCenterParams:
    """Instantiate `DataCenterParams` from the `data_center.toml` template (parsed TOML) for an
    obligation's committed kW, per its `[instantiation]` rules."""
    inst = profile["instantiation"]
    return DataCenterParams(
        deadband_kw=committed_kw * float(inst["deadband_fraction_of_committed"]),
        ramp_kw_per_min=committed_kw * float(inst["ramp_limit_per_committed_kw_per_min"]),
        freshness_s=float(profile["control"]["feedback_freshness_s"]),
        kp=kp,
    )


@dataclass(frozen=True, slots=True)
class DataCenterState:
    """One obligation's controller state, carried by the caller across cycles (one per obligation)."""

    output_kw: float = 0.0
    signal_lost_since_s: float | None = None


@dataclass(frozen=True, slots=True)
class DataCenterInputs:
    """This cycle's inputs. `target_import_kw` is the site-meter import to hold (`None`: no loop target
    declared, feed-forward only). `available_kw`: what the obligation's banks can deliver (K4)."""

    meter: FeedbackValue | None
    target_import_kw: float | None
    scheduled_kw: float
    committed_kw: float
    available_kw: float


@dataclass(frozen=True, slots=True)
class DataCenterOutput:
    setpoint_kw: float
    mode: ControlMode
    reason_code: str
    error_kw: float | None
    state: DataCenterState


def _deadband(error_kw: float, deadband_kw: float) -> float:
    if abs(error_kw) <= deadband_kw:
        return 0.0
    return error_kw - deadband_kw if error_kw > 0 else error_kw + deadband_kw


def step(
    inputs: DataCenterInputs, params: DataCenterParams, state: DataCenterState, *, now_s: float, dt_s: float
) -> DataCenterOutput:
    """One cycle of the DATA_CENTER controller (module docstring). Pure; `now_s` is an injected clock."""
    ceiling = envelope_ceiling_kw(inputs.committed_kw, inputs.available_kw)
    meter = inputs.meter
    if meter is not None and meter.usable:
        if inputs.target_import_kw is None:
            target, mode, reason, error = inputs.scheduled_kw, ControlMode.SCHEDULE, R_DC_NO_TARGET, None
        else:
            error = meter.value - inputs.target_import_kw
            target = inputs.scheduled_kw + params.kp * _deadband(error, params.deadband_kw)
            mode, reason = ControlMode.TRACKING, R_DC_TRACKING
        lost_since = None
    else:
        lost_since = state.signal_lost_since_s if state.signal_lost_since_s is not None else now_s
        error = None
        if now_s - lost_since < params.hold_s:
            target, mode, reason = state.output_kw, ControlMode.HOLD, R_DC_HOLD
        else:
            target, mode, reason = inputs.scheduled_kw, ControlMode.SCHEDULE, R_DC_SCHEDULE
    ramp_kw_per_s = params.ramp_kw_per_min / _SECONDS_PER_MINUTE
    ramped = apply_ramp_limit(state.output_kw, clamp(target, 0.0, ceiling), dt_s, ramp_kw_per_s)
    # A capacity or commitment drop binds at once: the ramp limit never holds the output above it.
    setpoint = clamp(ramped, 0.0, ceiling)
    return DataCenterOutput(setpoint, mode, reason, error, DataCenterState(setpoint, lost_since))


def with_delivered(state: DataCenterState, delivered_kw: float) -> DataCenterState:
    """Reconcile the controller with what the cycle actually granted (the tier allocation may deliver
    less than the setpoint), so a later HOLD holds the level really delivered."""
    return replace(state, output_kw=min(state.output_kw, max(delivered_kw, 0.0)))


def pcc_measurement(
    reading: SiteMeterReading, *, nominal_v: float, nominal_hz: float = NOMINAL_FREQ_HZ
) -> PqMeasurement:
    """The site meter as a `core.pq.PqMeasurement` for the S5.4 ladder. Imbalance is the worse of the
    voltage and current imbalance (S2 allows either definition; the worse is the conservative reading),
    voltage deviation the worst phase's |V - V_nominal| in % of nominal, current the highest phase."""
    if nominal_v <= 0:
        raise ValueError("nominal_v must be > 0")
    volts = {"A": reading.v_rms_a_v, "B": reading.v_rms_b_v, "C": reading.v_rms_c_v}
    amps = {"A": reading.i_rms_a_a, "B": reading.i_rms_b_a, "C": reading.i_rms_c_a}
    return PqMeasurement(
        imbalance_pct=max(phase_unbalance_pct(volts), phase_unbalance_pct(amps)),
        voltage_deviation_pct=max(abs(v - nominal_v) for v in volts.values()) / nominal_v * 100.0,
        freq_deviation_hz=abs(reading.freq_hz - nominal_hz),
        pf=abs(reading.pf),
        thd_voltage_pct=reading.thd_v_pct,
        thd_current_pct=reading.thd_i_pct,
        current_a=max(amps.values()),
    )


def usable_pcc_measurement(
    reading: SiteMeterReading | None, meter: FeedbackValue | None, *, nominal_v: float
) -> PqMeasurement | None:
    """`pcc_measurement` only when the reading is usable (GOOD and fresh per `meter`); otherwise `None`,
    which `closed_loop_common.monitor_pq` records as missing (S5.4), never as compliant."""
    if reading is None or meter is None or not meter.usable:
        return None
    return pcc_measurement(reading, nominal_v=nominal_v)
