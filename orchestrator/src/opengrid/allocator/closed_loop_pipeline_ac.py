"""PIPELINE_AC closed-loop controller: corridor current smoothing
(06-service-profiles-and-power-quality.md S4.a, 03-decision-engine.md S8.6.6; K9, K13, K14).

**Control law (S4.a, unchanged from 03 S8.6.6).** With a usable corridor-current reading `I_k` (GOOD,
no older than the freshness gate), a ramp-limited reference

    I~_k = I~_{k-1} + clip(I_k - I~_{k-1}, -r_A dt/60, +r_A dt/60)

and the fleet request through the shift factor `SF`

    r_k = clip(s_dir * sqrt(3) * V_kV * (I_k - I~_k) * PF / SF, -B, +B)    kW

The ramp-limit filter has no integrator and cannot hunt (S4.a "Stability notes"), so K9 holds: this is
not a second integrating loop on any quantity. The measured current is the corridor message's `i_ac_a`
(`interfaces/mqtt/pipeline_corridor_current.schema.json`).

**Envelope.** A committed obligation is a discharge grant, so the request is served inside
`[0, min(committed_kw, available_kw, B)]`: never above the commitment (K13) or deliverable capacity (K4).
A negative request (absorption, i.e. charging) cannot be carried by a discharge grant; it is served as
0 kW and reported as `absorption_unserved_kw`, never silently dropped.

**Signal loss** (`NEUTRAL_ON_SIGNAL_LOSS`, S4.a "Failure behaviour"): band to 0 kW, rate-limited by
`release_ramp_kw_per_min` when one is set; the reference restarts from the first usable reading after
recovery, so there is no step on resume. **Shift factor unknown** (03 S8.6.6): the customer's open-loop kW
schedule is run instead of the closed loop.

**PQ** (`corridor_measurement` + `closed_loop_common.monitor_pq`): the corridor current against the
tighter of the envelope's `current_limit_a` and the reading's own `limit_a` (`SPECIFIC_LINE` scope,
S4.a), fed to `pq_monitor`'s S5.4 ladder. The corridor signal measures nothing else, so the other
dimensions carry no information from it (0 ratio); THD on the corridor comes from waveform summaries.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Literal

from opengrid.allocator.closed_loop_common import ControlMode, clamp, envelope_ceiling_kw
from opengrid.core.physics import apply_ramp_limit
from opengrid.core.pq import PqEnvelopeLimits, PqMeasurement
from opengrid.site_ingest.models import CorridorCurrentReading, FeedbackValue

#: S4.a "Tuning": r_A, 30 A/min default (the 03 prototype value).
DEFAULT_RAMP_A_PER_MIN = 30.0
_SQRT3 = math.sqrt(3.0)
_SECONDS_PER_MINUTE = 60.0

R_PAC_SMOOTHING = "R-PAC-CORRIDOR-SMOOTHING"
R_PAC_NEUTRAL = "R-PAC-SIGNAL-LOSS-NEUTRAL"
R_PAC_OPEN_LOOP = "R-PAC-SHIFT-FACTOR-UNKNOWN-SCHEDULE"


@dataclass(frozen=True, slots=True)
class PipelineAcParams:
    """Per-obligation parameters: corridor model (V, PF, SF, direction) and contract (band, ramp)."""

    line_kv: float
    power_factor: float
    shift_factor: float | None
    direction: Literal[1, -1]
    band_kw: float
    freshness_s: float
    ramp_a_per_min: float = DEFAULT_RAMP_A_PER_MIN
    release_ramp_kw_per_min: float | None = None

    def __post_init__(self) -> None:
        if self.line_kv <= 0 or not 0 < self.power_factor <= 1 or self.band_kw < 0 or self.freshness_s <= 0:
            raise ValueError("line_kv > 0, 0 < power_factor <= 1, band_kw >= 0 and freshness_s > 0 required")
        if self.shift_factor is not None and self.shift_factor <= 0:
            raise ValueError("shift_factor must be > 0 when known")
        if self.ramp_a_per_min <= 0:
            raise ValueError("ramp_a_per_min must be > 0")

    @property
    def kw_per_amp(self) -> float:
        """sqrt(3) * V_kV * PF / SF: fleet kW per ampere of line-current deviation."""
        if self.shift_factor is None:
            raise ValueError("kw_per_amp is undefined without a shift factor")
        return _SQRT3 * self.line_kv * self.power_factor / self.shift_factor


@dataclass(frozen=True, slots=True)
class PipelineAcState:
    """Carried per obligation across cycles. `reference_a` is I~, `None` until a usable reading."""

    reference_a: float | None = None
    output_kw: float = 0.0


@dataclass(frozen=True, slots=True)
class PipelineAcInputs:
    current: FeedbackValue | None
    scheduled_kw: float
    committed_kw: float
    available_kw: float


@dataclass(frozen=True, slots=True)
class PipelineAcOutput:
    setpoint_kw: float
    requested_kw: float
    absorption_unserved_kw: float
    achieved_delta_a: float | None
    mode: ControlMode
    reason_code: str
    state: PipelineAcState


def _reference(previous_a: float | None, measured_a: float, max_step_a: float) -> float:
    if previous_a is None:
        return measured_a
    return previous_a + clamp(measured_a - previous_a, -max_step_a, max_step_a)


def step(
    inputs: PipelineAcInputs, params: PipelineAcParams, state: PipelineAcState, *, dt_s: float
) -> PipelineAcOutput:
    """One cycle of the PIPELINE_AC controller (module docstring). Pure."""
    reference = state.reference_a
    current = inputs.current
    if params.shift_factor is None:
        requested, mode, reason = inputs.scheduled_kw, ControlMode.OPEN_LOOP, R_PAC_OPEN_LOOP
    elif current is not None and current.usable:
        reference = _reference(
            state.reference_a, current.value, params.ramp_a_per_min * dt_s / _SECONDS_PER_MINUTE
        )
        raw_kw = params.direction * params.kw_per_amp * (current.value - reference)
        requested, mode, reason = (
            clamp(raw_kw, -params.band_kw, params.band_kw),
            ControlMode.TRACKING,
            R_PAC_SMOOTHING,
        )
    else:
        reference, requested, mode, reason = None, 0.0, ControlMode.NEUTRAL, R_PAC_NEUTRAL
    ceiling = envelope_ceiling_kw(inputs.committed_kw, inputs.available_kw, params.band_kw)
    served = clamp(requested, 0.0, ceiling)
    if params.release_ramp_kw_per_min is not None:
        served = apply_ramp_limit(
            state.output_kw, served, dt_s, params.release_ramp_kw_per_min / _SECONDS_PER_MINUTE
        )
    setpoint = clamp(served, 0.0, ceiling)
    return PipelineAcOutput(
        setpoint_kw=setpoint,
        requested_kw=requested,
        absorption_unserved_kw=max(-requested, 0.0),
        achieved_delta_a=setpoint / params.kw_per_amp if params.shift_factor is not None else None,
        mode=mode,
        reason_code=reason,
        state=PipelineAcState(reference_a=reference, output_kw=setpoint),
    )


def with_delivered(state: PipelineAcState, delivered_kw: float) -> PipelineAcState:
    """Reconcile the controller with what the cycle actually granted (see the DATA_CENTER twin)."""
    return replace(state, output_kw=min(state.output_kw, max(delivered_kw, 0.0)))


def corridor_limits(limits: PqEnvelopeLimits, reading: CorridorCurrentReading) -> PqEnvelopeLimits:
    """The envelope with its line-current cap set to the tighter of the contract's `current_limit_a`
    and the customer's reported `limit_a` (conservative when they disagree)."""
    caps = [c for c in (limits.current_limit_a, reading.limit_a) if c is not None]
    return replace(limits, current_limit_a=min(caps))


def corridor_measurement(reading: CorridorCurrentReading) -> PqMeasurement:
    """The corridor current as a `PqMeasurement` for the S5.4 ladder (only `current_a` is measured)."""
    return PqMeasurement(
        imbalance_pct=0.0,
        voltage_deviation_pct=0.0,
        freq_deviation_hz=0.0,
        pf=1.0,
        thd_voltage_pct=0.0,
        thd_current_pct=0.0,
        current_a=reading.i_ac_a,
    )
