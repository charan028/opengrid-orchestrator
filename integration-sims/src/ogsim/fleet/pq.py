"""ogsim.fleet.pq -- per-inverter power-quality imperfection model.

Implements `docs/orchestrator/07-delivery/06-service-profiles-and-power-quality.md`
§3.1 (per-inverter model) and §7.1/§7.3 (ogsim seeding/config) as ogsim's own,
independent code (BUILD.md §1: ogsim never imports opengrid). One `InverterUnit`
per hub, two for a dual-unit home (every 5th hub, `ogsim.fleet.state`'s
dual-unit rule) -- a 2-unit home's two inverters are recorded per hub, not
assumed to share a phase leg (§3.1).

Struct-of-arrays like `FleetState`/`FleetModifiers`, so a fleet-wide tick stays
vectorized. `PqAnomalyManager` mirrors `ogsim.fleet.anomalies.FleetAnomalyManager`'s
start/tick/accumulate_drift/revert shape (§7.2, §7.5) for the PQ-specific
anomaly types, plus `replace_inverter` for the `REPLACE_INVERTER` control
action (§7.5).

`inverter_state()` is the clean, WP-H-facing function: given a hub id, it
returns the per-unit parameters needed to synthesize a waveform (§7.4), without
this module ever generating samples itself (that is WP-H's job).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ogsim.fleet.state import FleetState

NOMINAL_FREQ_HZ = 60.0
HARMONIC_ORDERS: tuple[int, ...] = (3, 5, 7)
# Fraction of total THD_I attributed to each dominant odd harmonic order (§3.1
# "typically dominated by odd, non-triplen orders -- 3rd, 5th, 7th"), fixed
# shares that sum to 1.0.
_HARMONIC_SHARE: dict[int, float] = {3: 0.6, 5: 0.3, 7: 0.1}
# Fixed shared angle per order used when a fleet batch is harmonic-phase-locked
# (§3.2b stacking regime: identical PWM carrier phase across units).
_LOCKED_ANGLE_DEG: dict[int, float] = {3: 0.0, 5: 0.0, 7: 0.0}

PHASE_LEGS: tuple[str, ...] = ("A", "B", "C")

# §5.1 quality_score reference scales and equal-weight default.
_QUALITY_WEIGHT = 0.25
_FREQ_REF_HZ = 0.05
_VOLTAGE_REF_PCT = 2.0
_THD_REF_PCT = 5.0
_PHASE_ANGLE_REF_DEG = 10.0

# Ambient (non-anomalous) slow drift: a bounded Ornstein-Uhlenbeck-style walk
# around each unit's seeded baseline (thermal/aging variation, §7.1 "with a
# slow drift process"), not a hardware fault -- callers must not mistake this
# for a `calibration_drift_*` anomaly.
_AMBIENT_MEAN_REVERSION = 0.001  # per tick pull back toward the seeded baseline
_AMBIENT_FREQ_STEP_HZ = 0.0005
_AMBIENT_VOLTAGE_STEP_PCT = 0.02
_AMBIENT_PHASE_STEP_DEG = 0.02


@dataclass
class InverterPqState:
    """Struct-of-arrays state for every inverter unit in the fleet (one or two per hub)."""

    unit_ids: list[str]
    hub_ids: list[str]
    unit_index_in_hub: np.ndarray  # 0 or 1
    phase_connection: list[str]
    serial: list[str]
    firmware: list[str]

    freq_offset_hz: np.ndarray
    voltage_offset_pct: np.ndarray
    thd_current_pct: np.ndarray
    phase_angle_error_deg: np.ndarray
    response_time_ms: np.ndarray
    ride_through_class: list[str]

    # Seeded characterization, used as the ambient-drift mean-reversion target
    # and as the "new unit" values `replace_inverter` restores.
    freq_offset_baseline_hz: np.ndarray
    voltage_offset_baseline_pct: np.ndarray
    thd_current_baseline_pct: np.ndarray
    phase_angle_error_baseline_deg: np.ndarray

    # order -> (mag_pct[n], angle_deg[n])
    harmonic_mag_pct: dict[int, np.ndarray]
    harmonic_angle_deg: dict[int, np.ndarray]

    # Remote-calibration bookkeeping (§5.5.4): -1 = never calibrated/accepted.
    last_calibration_at: np.ndarray
    last_calibration_epoch: np.ndarray
    last_calibration_seq: np.ndarray

    hub_index: dict[str, list[int]] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.unit_ids)

    def indices_for_hub(self, hub_id: str) -> list[int]:
        return self.hub_index.get(hub_id, [])


def _hub_unit_layout(state: FleetState, dual_unit: np.ndarray) -> tuple[list[str], np.ndarray]:
    """Expands one row per hub into one or two rows per hub (dual-unit homes),
    returning the parallel `hub_ids` array and each unit's 0/1 index within its hub."""
    hub_ids: list[str] = []
    unit_index: list[int] = []
    for hub_id, is_dual in zip(state.hub_ids, dual_unit, strict=True):
        hub_ids.append(hub_id)
        unit_index.append(0)
        if is_dual:
            hub_ids.append(hub_id)
            unit_index.append(1)
    return hub_ids, np.array(unit_index, dtype=np.int64)


def _assign_phase_connection(hub_ids: list[str], unit_index: np.ndarray, rng: np.random.Generator) -> list[str]:
    """Single-unit homes are assigned a leg round-robin by hub position (spreads
    single-phase load evenly across A/B/C, §3.1); a dual-unit home's second
    inverter is recorded independently -- a seeded coin flip decides whether it
    shares its sibling's leg or sits on a different one (§3.1: "recorded per
    hub, not assumed")."""
    seen_hubs: dict[str, str] = {}
    legs: list[str] = []
    for pos, (hub_id, idx) in enumerate(zip(hub_ids, unit_index, strict=True)):
        if idx == 0:
            leg = PHASE_LEGS[pos % len(PHASE_LEGS)]
            seen_hubs[hub_id] = leg
        else:
            sibling_leg = seen_hubs[hub_id]
            if rng.random() < 0.5:
                leg = sibling_leg
            else:
                other_legs = [phase_leg for phase_leg in PHASE_LEGS if phase_leg != sibling_leg]
                leg = other_legs[rng.integers(0, len(other_legs))]
        legs.append(leg)
    return legs


def _draw_harmonics(
    n: int, thd_current_pct: np.ndarray, harmonic_phase_lock: bool, rng: np.random.Generator
) -> tuple[dict[int, np.ndarray], dict[int, np.ndarray]]:
    mag_pct: dict[int, np.ndarray] = {}
    angle_deg: dict[int, np.ndarray] = {}
    for order, share in _HARMONIC_SHARE.items():
        mag_pct[order] = thd_current_pct * share
        if harmonic_phase_lock:
            angle_deg[order] = np.full(n, _LOCKED_ANGLE_DEG[order])
        else:
            angle_deg[order] = rng.uniform(0.0, 360.0, size=n)
    return mag_pct, angle_deg


def build_inverter_pq_state(config: Any, state: FleetState, rng: np.random.Generator) -> InverterPqState:
    """Seeds one `InverterPqState` row per inverter unit (§7.1), from
    `config`'s `pq_*` fields (loaded from `fleet.yaml`'s `inverter_pq:` block,
    §7.3). `state` supplies dual-unit membership (via `e_kwh == config.e_kwh_dual_unit`,
    the same test used by `test_fleet_dual_unit.py`) so the two products' hub
    counts always agree without sharing code (BUILD.md §1)."""
    dual_unit = state.e_kwh == config.e_kwh_dual_unit
    hub_ids, unit_index = _hub_unit_layout(state, dual_unit)
    n = len(hub_ids)

    freq_offset = rng.normal(0.0, config.pq_freq_offset_std_hz, size=n)
    voltage_offset = rng.normal(0.0, config.pq_voltage_offset_std_pct, size=n)
    # Log-normal THD_I: parameterized so the median matches `thd_current_median_pct`
    # (mu = ln(median)) and the spread matches the configured p95 (§7.1).
    median = max(config.pq_thd_current_median_pct, 1e-6)
    p95 = max(config.pq_thd_current_p95_pct, median * 1.01)
    sigma = float(np.log(p95 / median) / 1.6448536269514722)  # z_0.95
    thd_current = rng.lognormal(mean=np.log(median), sigma=sigma, size=n)
    phase_angle_error = rng.normal(0.0, config.pq_voltage_offset_std_pct * 2.0, size=n)
    response_time = np.maximum(rng.normal(200.0, 20.0, size=n), 20.0)

    harmonic_mag, harmonic_angle = _draw_harmonics(n, thd_current, config.pq_harmonic_phase_lock, rng)

    unit_ids = [f"{hub_id}-inv{idx}" for hub_id, idx in zip(hub_ids, unit_index, strict=True)]
    serials = [f"SN-{unit_id}-0000" for unit_id in unit_ids]
    firmware = ["FW-1.0.0"] * n

    hub_index: dict[str, list[int]] = {}
    for i, hub_id in enumerate(hub_ids):
        hub_index.setdefault(hub_id, []).append(i)

    return InverterPqState(
        unit_ids=unit_ids,
        hub_ids=hub_ids,
        unit_index_in_hub=unit_index,
        phase_connection=_assign_phase_connection(hub_ids, unit_index, rng),
        serial=serials,
        firmware=firmware,
        freq_offset_hz=freq_offset,
        voltage_offset_pct=voltage_offset,
        thd_current_pct=thd_current,
        phase_angle_error_deg=phase_angle_error,
        response_time_ms=response_time,
        ride_through_class=[config.pq_ride_through_class_default] * n,
        freq_offset_baseline_hz=freq_offset.copy(),
        voltage_offset_baseline_pct=voltage_offset.copy(),
        thd_current_baseline_pct=thd_current.copy(),
        phase_angle_error_baseline_deg=phase_angle_error.copy(),
        harmonic_mag_pct=harmonic_mag,
        harmonic_angle_deg=harmonic_angle,
        last_calibration_at=np.full(n, -1.0),
        last_calibration_epoch=np.full(n, -1, dtype=np.int64),
        last_calibration_seq=np.full(n, -1, dtype=np.int64),
        hub_index=hub_index,
    )


def quality_score(
    freq_offset_hz: np.ndarray,
    voltage_offset_pct: np.ndarray,
    thd_current_pct: np.ndarray,
    phase_angle_error_deg: np.ndarray,
) -> np.ndarray:
    """§5.1's scalar quality summary, clipped to [0, 1]."""
    score = (
        1.0
        - _QUALITY_WEIGHT * np.abs(freq_offset_hz) / _FREQ_REF_HZ
        - _QUALITY_WEIGHT * np.abs(voltage_offset_pct) / _VOLTAGE_REF_PCT
        - _QUALITY_WEIGHT * thd_current_pct / _THD_REF_PCT
        - _QUALITY_WEIGHT * np.abs(phase_angle_error_deg) / _PHASE_ANGLE_REF_DEG
    )
    return np.clip(score, 0.0, 1.0)


def _ou_step(value: np.ndarray, baseline: np.ndarray, step_std: float, rng: np.random.Generator) -> np.ndarray:
    noise = rng.normal(0.0, step_std, size=value.shape)
    return baseline + (value - baseline) * (1.0 - _AMBIENT_MEAN_REVERSION) + noise


def tick_ambient_drift(pq: InverterPqState, dt_s: float, rng: np.random.Generator) -> None:
    """Advances the slow, bounded ambient drift process (§7.1) every fleet
    tick, independent of any injected anomaly. Scaled by `sqrt(dt_s)` so the
    walk's variance accumulates correctly regardless of tick length."""
    scale = float(np.sqrt(max(dt_s, 0.0) / 3600.0))
    pq.freq_offset_hz = _ou_step(
        pq.freq_offset_hz, pq.freq_offset_baseline_hz, _AMBIENT_FREQ_STEP_HZ * scale, rng
    )
    pq.voltage_offset_pct = _ou_step(
        pq.voltage_offset_pct, pq.voltage_offset_baseline_pct, _AMBIENT_VOLTAGE_STEP_PCT * scale, rng
    )
    pq.phase_angle_error_deg = _ou_step(
        pq.phase_angle_error_deg,
        pq.phase_angle_error_baseline_deg,
        _AMBIENT_PHASE_STEP_DEG * scale,
        rng,
    )


@dataclass(frozen=True)
class InverterSnapshot:
    """The parameters WP-H needs to synthesize one inverter's waveform (§7.4).
    Immutable, decoupled from `InverterPqState`'s numpy storage."""

    unit_id: str
    hub_id: str
    phase_connection: str
    freq_hz: float
    voltage_offset_pct: float
    thd_current_pct: float
    dominant_harmonics: dict[int, dict[str, float]]  # order -> {mag_pct, angle_deg}
    phase_angle_error_deg: float
    response_time_ms: float
    ride_through_class: str
    quality_score: float


def inverter_state(pq: InverterPqState, hub_id: str) -> list[InverterSnapshot]:
    """Returns one `InverterSnapshot` per unit on `hub_id` (one, or two for a
    dual-unit home). This is the function WP-H (waveform generation, later)
    calls; it never generates samples itself."""
    scores = quality_score(pq.freq_offset_hz, pq.voltage_offset_pct, pq.thd_current_pct, pq.phase_angle_error_deg)
    snapshots = []
    for i in pq.indices_for_hub(hub_id):
        harmonics = {
            order: {"mag_pct": float(pq.harmonic_mag_pct[order][i]), "angle_deg": float(pq.harmonic_angle_deg[order][i])}
            for order in HARMONIC_ORDERS
        }
        snapshots.append(
            InverterSnapshot(
                unit_id=pq.unit_ids[i],
                hub_id=hub_id,
                phase_connection=pq.phase_connection[i],
                freq_hz=NOMINAL_FREQ_HZ + float(pq.freq_offset_hz[i]),
                voltage_offset_pct=float(pq.voltage_offset_pct[i]),
                thd_current_pct=float(pq.thd_current_pct[i]),
                dominant_harmonics=harmonics,
                phase_angle_error_deg=float(pq.phase_angle_error_deg[i]),
                response_time_ms=float(pq.response_time_ms[i]),
                ride_through_class=pq.ride_through_class[i],
                quality_score=float(scores[i]),
            )
        )
    return snapshots


# ---------------------------------------------------------------------------
# PQ-specific anomalies (§7.2, §7.5) and REPLACE_INVERTER (§7.5).
# ---------------------------------------------------------------------------

PQ_ANOMALY_TYPES = frozenset(
    {"frequency_drift", "harmonic_injection", "phase_imbalance_injection", "calibration_drift_correctable",
     "calibration_drift_hardware"}
)


@dataclass
class ActivePqAnomaly:
    id: str
    type: str
    unit_indices: list[int]
    params: dict[str, Any]
    start: float
    duration: float | None
    start_freq_offset_hz: np.ndarray
    start_voltage_offset_pct: np.ndarray
    start_thd_current_pct: np.ndarray

    def is_active_at(self, now: float) -> bool:
        if now < self.start:
            return False
        return self.duration is None or now < self.start + self.duration

    def progress(self, now: float) -> float:
        """Linear ramp fraction in [0, 1] from `start` to `start + duration`."""
        if self.duration is None or self.duration <= 0:
            return 1.0
        return float(np.clip((now - self.start) / self.duration, 0.0, 1.0))


class PqAnomalyManager:
    """Applies/reverts the PQ-specific fleet anomalies (§7.2, §7.5) against an
    `InverterPqState`, mirroring `ogsim.fleet.anomalies.FleetAnomalyManager`'s
    start/tick shape. `drift_correctable` tracks, per unit, whether the
    currently active drift (if any) is `calibration_drift_correctable` (True)
    or `calibration_drift_hardware` (False); `None` means no active
    calibration-drift anomaly on that unit.
    """

    def __init__(self, pq: InverterPqState) -> None:
        self.pq = pq
        self.drift_correctable: list[bool | None] = [None] * pq.n
        self.dispatch_bias_kw = np.zeros(pq.n)
        self._active: dict[str, ActivePqAnomaly] = {}

    def _resolve_units(self, target_kind: str | None, target_ref: str) -> list[int]:
        pq = self.pq
        if target_ref in ("*", "") or target_kind == "sim":
            return list(range(pq.n))
        if target_kind == "hub" or target_ref in pq.hub_index:
            return list(pq.indices_for_hub(target_ref))
        return []

    def start(
        self,
        anomaly_id: str,
        anomaly_type: str,
        target_kind: str | None,
        target_ref: str,
        params: dict[str, Any],
        start: float,
        duration_s: float | None,
    ) -> ActivePqAnomaly:
        idx = self._resolve_units(target_kind, target_ref)
        anomaly = ActivePqAnomaly(
            anomaly_id,
            anomaly_type,
            idx,
            params,
            start,
            duration_s,
            self.pq.freq_offset_hz[idx].copy(),
            self.pq.voltage_offset_pct[idx].copy(),
            self.pq.thd_current_pct[idx].copy(),
        )
        self._active[anomaly_id] = anomaly
        if anomaly_type == "calibration_drift_correctable":
            for i in idx:
                self.drift_correctable[i] = True
        elif anomaly_type == "calibration_drift_hardware":
            for i in idx:
                self.drift_correctable[i] = False
        elif anomaly_type == "phase_imbalance_injection":
            self.dispatch_bias_kw[idx] = float(params.get("bias_kw", 2.0))
        self._apply_ramp(anomaly, start)
        return anomaly

    def _apply_ramp(self, anomaly: ActivePqAnomaly, now: float) -> None:
        """Ramps the anomaly's target field(s) linearly toward `params`'
        target value(s) as `now` advances through the anomaly's window."""
        idx = anomaly.unit_indices
        if not idx:
            return
        fraction = anomaly.progress(now)
        pq = self.pq
        if anomaly.type == "frequency_drift":
            target = float(anomaly.params.get("target_offset_hz", 0.3))
            pq.freq_offset_hz[idx] = anomaly.start_freq_offset_hz + fraction * (
                target - anomaly.start_freq_offset_hz
            )
        elif anomaly.type == "harmonic_injection":
            target = float(anomaly.params.get("thd_target_pct", 8.0))
            pq.thd_current_pct[idx] = anomaly.start_thd_current_pct + fraction * (
                target - anomaly.start_thd_current_pct
            )
            order = int(anomaly.params.get("order", 5))
            if order in pq.harmonic_mag_pct:
                pq.harmonic_mag_pct[order][idx] = pq.thd_current_pct[idx] * 0.8
        elif anomaly.type in ("calibration_drift_correctable", "calibration_drift_hardware"):
            target_freq = float(anomaly.params.get("target_freq_offset_hz", 0.2))
            target_voltage = float(anomaly.params.get("target_voltage_offset_pct", 2.0))
            pq.freq_offset_hz[idx] = anomaly.start_freq_offset_hz + fraction * (
                target_freq - anomaly.start_freq_offset_hz
            )
            pq.voltage_offset_pct[idx] = anomaly.start_voltage_offset_pct + fraction * (
                target_voltage - anomaly.start_voltage_offset_pct
            )
        # phase_imbalance_injection: dispatch_bias_kw is set once in `start`,
        # not ramped -- it models a fixed skew the fleet engine adds each tick.

    def tick(self, now: float) -> None:
        """Advances active ramps and reverts anomalies whose duration elapsed.

        `frequency_drift`/`harmonic_injection`/`phase_imbalance_injection`
        revert to their pre-anomaly value on expiry (a transient fault
        clears). `calibration_drift_*` anomalies instead freeze at their
        last ramped value on expiry: they model a persistent characterization
        fault that only a calibration command or `REPLACE_INVERTER` removes,
        never mere time elapsing (§5.5.4) -- but the unit's
        `drift_correctable` flag no longer applies once the anomaly itself is
        gone, since nothing is actively driving further drift.
        """
        for anomaly in list(self._active.values()):
            if not anomaly.is_active_at(now):
                self._expire(anomaly)
                del self._active[anomaly.id]
            else:
                self._apply_ramp(anomaly, now)

    def _expire(self, anomaly: ActivePqAnomaly) -> None:
        idx = anomaly.unit_indices
        pq = self.pq
        if anomaly.type == "frequency_drift":
            pq.freq_offset_hz[idx] = anomaly.start_freq_offset_hz
        elif anomaly.type == "harmonic_injection":
            pq.thd_current_pct[idx] = anomaly.start_thd_current_pct
        elif anomaly.type == "phase_imbalance_injection":
            self.dispatch_bias_kw[idx] = 0.0
        elif anomaly.type in ("calibration_drift_correctable", "calibration_drift_hardware"):
            for i in idx:
                self.drift_correctable[i] = None

    def active_ids(self) -> list[str]:
        return list(self._active.keys())

    def has_active_calibration_drift(self, unit_index: int) -> bool:
        return self.drift_correctable[unit_index] is not None

    def clear_anomalies_touching(self, unit_indices: list[int]) -> None:
        """Removes any active anomaly whose target set intersects
        `unit_indices` (used by `replace_inverter`, §7.5), without touching
        other units a shared anomaly (e.g. a zone-wide one) may also cover."""
        stale = [a.id for a in self._active.values() if any(i in unit_indices for i in a.unit_indices)]
        for anomaly_id in stale:
            del self._active[anomaly_id]


def replace_inverter(
    pq: InverterPqState,
    anomalies: PqAnomalyManager,
    hub_id: str,
    new_serial: str,
    new_firmware: str,
    rng: np.random.Generator,
) -> None:
    """`REPLACE_INVERTER` control action (§7.5): resets every offset on
    `hub_id`'s unit(s) to freshly-drawn "new unit" values (as if seeded at
    t=0, using this same unit's baseline standard deviations), records the
    new serial/firmware, and clears any active `calibration_drift_*` anomaly
    on that hub so the state machine can drive `QUARANTINED ->
    AWAITING_REPLACEMENT -> RECOMMISSIONING -> OK` end-to-end in tests."""
    idx = pq.indices_for_hub(hub_id)
    if not idx:
        raise ValueError(f"unknown hub_id for REPLACE_INVERTER: {hub_id!r}")
    n = len(idx)
    freq_std = float(np.std(pq.freq_offset_baseline_hz)) or 0.01
    voltage_std = float(np.std(pq.voltage_offset_baseline_pct)) or 0.5
    new_freq = rng.normal(0.0, freq_std, size=n)
    new_voltage = rng.normal(0.0, voltage_std, size=n)
    new_phase_angle = rng.normal(0.0, voltage_std * 2.0, size=n)
    new_thd = np.abs(rng.normal(pq.thd_current_baseline_pct[idx].mean(), 0.5, size=n))

    for pos, i in enumerate(idx):
        pq.freq_offset_hz[i] = new_freq[pos]
        pq.freq_offset_baseline_hz[i] = new_freq[pos]
        pq.voltage_offset_pct[i] = new_voltage[pos]
        pq.voltage_offset_baseline_pct[i] = new_voltage[pos]
        pq.phase_angle_error_deg[i] = new_phase_angle[pos]
        pq.phase_angle_error_baseline_deg[i] = new_phase_angle[pos]
        pq.thd_current_pct[i] = new_thd[pos]
        pq.thd_current_baseline_pct[i] = new_thd[pos]
        pq.serial[i] = new_serial if n == 1 else f"{new_serial}-{pq.unit_index_in_hub[i]}"
        pq.firmware[i] = new_firmware
        pq.last_calibration_at[i] = -1.0
        pq.last_calibration_epoch[i] = -1
        pq.last_calibration_seq[i] = -1
        anomalies.drift_correctable[i] = None

    anomalies.clear_anomalies_touching(idx)
