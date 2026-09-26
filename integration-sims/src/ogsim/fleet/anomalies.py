"""ogsim.fleet.anomalies -- FLEET_* anomaly catalogue application/reversion.

Driven by `<root>/scenario/cmd` (BUILD.md §3, parsed tolerantly by
`ogsim.common.scenario`). Each active anomaly is tracked with its
id/start/duration; `tick()` applies effects for anomalies that have
started and not yet ended, and reverts (restores defaults) the instant an
anomaly's duration elapses -- so every type both "applies and reverts
cleanly" as BUILD.md requires.

Anomaly effects are expressed as per-hub numpy modifier arrays layered on
top of `FleetState` rather than one-off flags, so `physics.tick()` and
telemetry publishing just read these arrays uniformly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ogsim.fleet.state import HEALTH_FAULT, HEALTH_ONLINE, FleetState

logger = logging.getLogger(__name__)

FLEET_ANOMALY_TYPES = frozenset(
    {
        "hub_offline",
        "zone_mass_disconnect",
        "not_following_commands",
        "inverter_trip",
        "soc_sensor_drift",
        "telemetry_delay_burst",
        "lease_loss",
        "clock_skew",
        "tampered_unsigned_command",
        "reserve_floor_pressure",
    }
)


@dataclass
class ActiveAnomaly:
    id: str
    type: str
    hub_indices: list[int]
    params: dict[str, Any]
    start: float
    duration: float | None

    def is_active_at(self, now: float) -> bool:
        if now < self.start:
            return False
        return self.duration is None or now < self.start + self.duration


@dataclass
class FleetModifiers:
    """Per-hub effect arrays, defaulting to "no anomaly in progress"."""

    n: int
    follow_fraction: np.ndarray = field(init=False)
    soc_drift_kwh: np.ndarray = field(init=False)
    clock_skew_s: np.ndarray = field(init=False)
    telemetry_suppressed: np.ndarray = field(init=False)
    telemetry_buffer: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    forced_home_load_kw: np.ndarray = field(init=False)
    inverter_tripped: np.ndarray = field(init=False)
    force_lease_expire: set[int] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.follow_fraction = np.ones(self.n)
        self.soc_drift_kwh = np.zeros(self.n)
        self.clock_skew_s = np.zeros(self.n)
        self.telemetry_suppressed = np.zeros(self.n, dtype=bool)
        self.forced_home_load_kw = np.full(self.n, np.nan)
        self.inverter_tripped = np.zeros(self.n, dtype=bool)


class FleetAnomalyManager:
    """Applies/reverts FLEET_* anomalies against a `FleetState`."""

    def __init__(self, state: FleetState) -> None:
        self.state = state
        self.modifiers = FleetModifiers(len(state.hub_ids))
        self._active: dict[str, ActiveAnomaly] = {}

    def _resolve_targets(self, target_kind: str | None, target_ref: str) -> list[int]:
        state = self.state
        if target_ref in ("*", "") or target_kind == "sim":
            return list(range(len(state.hub_ids)))
        if target_kind == "hub" or target_ref in state.hub_index:
            idx = state.hub_index.get(target_ref)
            return [idx] if idx is not None else []
        if target_kind == "zone" or target_ref in state.zones:
            return [i for i, z in enumerate(state.zones) if z == target_ref]
        if target_kind == "bank" or target_ref in state.bank_ids:
            return [i for i, b in enumerate(state.bank_ids) if b == target_ref]
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
    ) -> ActiveAnomaly:
        indices = self._resolve_targets(target_kind, target_ref)
        if not indices:
            # Not an error (behaviour unchanged), but an unknown target silently does nothing.
            logger.warning(
                "anomaly %s (%s) target kind=%s ref=%r matched 0 hubs; it has no effect",
                anomaly_id,
                anomaly_type,
                target_kind,
                target_ref,
            )
        anomaly = ActiveAnomaly(anomaly_id, anomaly_type, indices, params, start, duration_s)
        self._active[anomaly_id] = anomaly
        self._apply(anomaly)
        return anomaly

    def tick(self, now: float) -> None:
        """Reverts anomalies whose duration has elapsed. Ongoing effects
        (drift accumulation) are advanced by the caller each tick via
        `accumulate_drift`."""
        expired = [a for a in self._active.values() if not a.is_active_at(now)]
        for anomaly in expired:
            self._revert(anomaly)
            del self._active[anomaly.id]

    def accumulate_drift(self, dt_s: float) -> None:
        for anomaly in self._active.values():
            if anomaly.type != "soc_sensor_drift":
                continue
            rate = float(anomaly.params.get("drift_kwh_per_min", 0.5)) / 60.0
            idx = anomaly.hub_indices
            self.modifiers.soc_drift_kwh[idx] += rate * dt_s

    def _apply(self, anomaly: ActiveAnomaly) -> None:
        idx = anomaly.hub_indices
        m = self.modifiers
        state = self.state
        if anomaly.type in ("hub_offline", "zone_mass_disconnect"):
            m.telemetry_suppressed[idx] = True
        elif anomaly.type == "not_following_commands":
            fraction = 0.5 if anomaly.params.get("mode", "partial") == "partial" else 0.0
            m.follow_fraction[idx] = fraction
        elif anomaly.type == "inverter_trip":
            m.inverter_tripped[idx] = True
            for i in idx:
                state.health[i] = HEALTH_FAULT
                state.fault_code[i] = "INVERTER_TRIP"
        elif anomaly.type == "telemetry_delay_burst":
            m.telemetry_suppressed[idx] = True
            for i in idx:
                m.telemetry_buffer.setdefault(i, [])
        elif anomaly.type == "lease_loss":
            m.force_lease_expire.update(idx)
        elif anomaly.type == "clock_skew":
            m.clock_skew_s[idx] = float(anomaly.params.get("skew_s", 300.0))
        elif anomaly.type == "reserve_floor_pressure":
            m.forced_home_load_kw[idx] = float(anomaly.params.get("home_load_kw", 6.0))
        # soc_sensor_drift: handled incrementally by accumulate_drift.
        # tampered_unsigned_command: a fleet self-test, applied by the
        # runtime's ack path, not a persistent state modifier.

    def _revert(self, anomaly: ActiveAnomaly) -> None:
        idx = anomaly.hub_indices
        m = self.modifiers
        state = self.state
        if anomaly.type in ("hub_offline", "zone_mass_disconnect"):
            m.telemetry_suppressed[idx] = False
        elif anomaly.type == "not_following_commands":
            m.follow_fraction[idx] = 1.0
        elif anomaly.type == "inverter_trip":
            m.inverter_tripped[idx] = False
            for i in idx:
                state.health[i] = HEALTH_ONLINE
                state.fault_code[i] = None
        elif anomaly.type == "telemetry_delay_burst":
            m.telemetry_suppressed[idx] = False
            for i in idx:
                m.telemetry_buffer.pop(i, None)
        elif anomaly.type == "clock_skew":
            m.clock_skew_s[idx] = 0.0
        elif anomaly.type == "reserve_floor_pressure":
            m.forced_home_load_kw[idx] = np.nan
        elif anomaly.type == "soc_sensor_drift":
            m.soc_drift_kwh[idx] = 0.0

    def active_ids(self) -> list[str]:
        return list(self._active.keys())
