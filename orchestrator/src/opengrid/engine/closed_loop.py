"""Closed-loop controllers in the allocator cycle (06-service-profiles-and-power-quality.md S4.a/S4.b; need
basis, owner decision 2026-09-26; K9, K13, K14).

For every DATA_CENTER / PIPELINE_AC obligation that is COMMITTED, DELIVERING or SHORTFALL with its window
open, each cycle:

1. resolves the profile's `feedback_signal_ref` for the obligation's customer (`opengrid.site_ingest`);
2. steps the profile's controller (`allocator.closed_loop_data_center` / `closed_loop_pipeline_ac`) with
   the obligation's committed kW as the ceiling and feed-forward, and what its hubs can deliver now;
3. splits the setpoint across the obligation's banks (`closed_loop_common.closed_loop_caps`): the cycle's
   `closed_loop_caps`, granted with R-GRANT-CLOSED-LOOP. The unused commitment stays idle (K13).

After the cycle, each controller is reconciled with what was actually granted. The PCC measurement (site
meter / corridor current) is handed to the S5.4 ladder whenever site ingest is on, even with control off.

Control runs only behind `[allocator.closed_loop].enabled` (default off, the owner decides); signals only
behind `[site_ingest].enabled`.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg_pool import AsyncConnectionPool

from opengrid.allocator import closed_loop_data_center as dc
from opengrid.allocator import closed_loop_pipeline_ac as pac
from opengrid.allocator.closed_loop_common import closed_loop_caps
from opengrid.allocator.models import CycleResult, FleetState, LedgerView, ObligationCall
from opengrid.core.pq import PqEnvelopeLimits, PqMeasurement
from opengrid.core.services import DATA_CENTER_SERVICE_TYPE, PIPELINE_AC_SERVICE_TYPE
from opengrid.engine.settings import DispatchSettings
from opengrid.site_ingest import (
    CorridorCurrentReading,
    FeedbackRefError,
    FeedbackValue,
    SiteMeterReading,
    parse_feedback_ref,
)

logger = logging.getLogger(__name__)

CLOSED_LOOP_SERVICES = frozenset({DATA_CENTER_SERVICE_TYPE, PIPELINE_AC_SERVICE_TYPE})
#: How often the obligations' profile/envelope rows are re-read (not every 2 s cycle).
SPEC_REFRESH_S = 10.0
_DEFAULT_CYCLE_S = 2.0

#: Every closed-loop obligation with its window open now, its customer, its latest profile's feedback
#: signal and its PQ envelope limits.
_SPECS_SQL = """
SELECT o.obligation_id, o.service_type, c.customer_id, sp.feedback_signal_ref,
       e.max_phase_imbalance_pct, e.voltage_band_pct, e.freq_tolerance_hz, e.pf_min,
       e.thd_voltage_limit_pct, e.thd_current_limit_pct, e.current_limit_a
FROM og.obligation o
JOIN og.contract c ON c.contract_id = o.contract_id
LEFT JOIN LATERAL (
    SELECT s.feedback_signal_ref, s.pq_envelope_id FROM og.service_profile s
    WHERE s.contract_id = o.contract_id ORDER BY s.version DESC LIMIT 1
) sp ON true
LEFT JOIN og.pq_envelope e ON e.pq_envelope_id = sp.pq_envelope_id
WHERE o.service_type IN ('DATA_CENTER', 'PIPELINE_AC')
  AND o.state IN ('COMMITTED', 'DELIVERING', 'SHORTFALL')
  AND o.window_start <= %(now)s AND o.window_end > %(now)s
"""


@dataclass(frozen=True, slots=True)
class ClosedLoopSpec:
    obligation_id: str
    service_type: str
    customer_id: str
    feedback_signal_ref: str | None
    limits: PqEnvelopeLimits | None


def spec_from_row(row: tuple[Any, ...]) -> ClosedLoopSpec:
    oid, service_type, customer_id, ref, imb, vband, ftol, pf_min, thd_v, thd_i, current = row
    limits = (
        PqEnvelopeLimits(
            max_phase_imbalance_pct=float(imb),
            voltage_band_pct=float(vband),
            freq_tolerance_hz=float(ftol),
            pf_min=float(pf_min),
            thd_voltage_limit_pct=float(thd_v),
            thd_current_limit_pct=float(thd_i),
            current_limit_a=float(current) if current is not None else None,
        )
        if imb is not None
        else None
    )
    return ClosedLoopSpec(str(oid), str(service_type), str(customer_id), ref, limits)


SpecLoader = Callable[[datetime], Awaitable[list[ClosedLoopSpec]]]


def pg_spec_loader(pool: AsyncConnectionPool) -> SpecLoader:
    async def _load(now: datetime) -> list[ClosedLoopSpec]:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_SPECS_SQL, {"now": now})
            rows = await cur.fetchall()
        return [spec_from_row(tuple(r)) for r in rows]

    return _load


@dataclass(frozen=True, slots=True)
class SiteSignals:
    """The `opengrid.site_ingest` reads the runner needs (injected so tests need no ingest state)."""

    resolve: Callable[..., FeedbackValue | None]
    latest_site: Callable[[str, str], SiteMeterReading | None]
    latest_corridor: Callable[[str, str], CorridorCurrentReading | None]


def default_site_signals() -> SiteSignals:
    from opengrid import site_ingest

    return SiteSignals(
        resolve=site_ingest.resolve_feedback_signal,
        latest_site=site_ingest.latest_site_meter,
        latest_corridor=site_ingest.latest_corridor_current,
    )


@dataclass(frozen=True, slots=True)
class ModeChange:
    obligation_id: str
    mode: str
    reason_code: str


class ClosedLoopRunner:
    """One controller state per closed-loop obligation, kept across cycles by the og-engine process."""

    def __init__(
        self,
        load_specs: SpecLoader,
        settings: DispatchSettings,
        *,
        dc_profile: Mapping[str, Any] | None,
        signals: SiteSignals,
        control_enabled: bool,
        refresh_s: float = SPEC_REFRESH_S,
    ) -> None:
        self._load_specs = load_specs
        self._settings = settings
        self._dc_profile = dc_profile
        self._signals = signals
        self.control_enabled = control_enabled
        self._refresh_s = refresh_s
        self._specs: dict[str, ClosedLoopSpec] = {}
        self._loaded_at: datetime | None = None
        self._dc_states: dict[str, dc.DataCenterState] = {}
        self._pac_states: dict[str, pac.PipelineAcState] = {}
        self._stepped_at: dict[str, datetime] = {}
        self._modes: dict[str, str] = {}
        self.mode_changes: list[ModeChange] = []

    @property
    def specs(self) -> Mapping[str, ClosedLoopSpec]:
        return self._specs

    async def refresh(self, now: datetime) -> None:
        if self._loaded_at is not None and (now - self._loaded_at).total_seconds() < self._refresh_s:
            return
        try:
            specs = await self._load_specs(now)
        except Exception:
            logger.exception("closed-loop obligations unreadable; keeping the last set")
            return
        self._loaded_at = now
        self._specs = {s.obligation_id: s for s in specs}
        for states in (self._dc_states, self._pac_states, self._stepped_at, self._modes):
            for oid in set(states) - set(self._specs):
                del states[oid]

    async def caps(
        self, fleet_state: FleetState, ledger_view: LedgerView, now: datetime
    ) -> dict[tuple[str, str], float]:
        """This cycle's `closed_loop_caps` (empty while control is off)."""
        await self.refresh(now)
        self.mode_changes = []
        if not self.control_enabled or not self._specs:
            return {}
        calls_by_oid: dict[str, list[ObligationCall]] = {}
        for call in ledger_view.calls:
            if call.obligation_id in self._specs:
                calls_by_oid.setdefault(call.obligation_id, []).append(call)
        if not calls_by_oid:
            return {}
        free_by_hub = {h.hub_id: h.free_discharge_kw for h in fleet_state.hubs if h.is_healthy}
        setpoints: dict[str, float] = {}
        for oid, calls in calls_by_oid.items():
            committed = sum(max(c.committed_kw, 0.0) for c in calls)
            available = sum(free_by_hub.get(h, 0.0) for c in calls for h in c.eligible_hub_ids)
            last = self._stepped_at.get(oid)
            dt_s = (now - last).total_seconds() if last is not None else _DEFAULT_CYCLE_S
            self._stepped_at[oid] = now
            setpoint = self._step(self._specs[oid], committed, available, now, max(dt_s, 0.0))
            if setpoint is not None:
                setpoints[oid] = setpoint
        return closed_loop_caps([c for calls in calls_by_oid.values() for c in calls], setpoints)

    def _feedback(self, spec: ClosedLoopSpec, max_age_s: float, now: datetime) -> FeedbackValue | None:
        if not spec.feedback_signal_ref:
            return None
        try:
            return self._signals.resolve(
                spec.feedback_signal_ref, customer_id=spec.customer_id, max_age_s=max_age_s, now=now
            )
        except FeedbackRefError:
            logger.warning("malformed feedback_signal_ref", extra={"obligation_id": spec.obligation_id})
            return None

    def _step(
        self, spec: ClosedLoopSpec, committed: float, available: float, now: datetime, dt_s: float
    ) -> float | None:
        oid = spec.obligation_id
        if spec.service_type == DATA_CENTER_SERVICE_TYPE:
            params = self._dc_params(committed)
            site_id = _source_id(spec.feedback_signal_ref)
            out = dc.step(
                dc.DataCenterInputs(
                    meter=self._feedback(spec, params.freshness_s, now),
                    target_import_kw=self._settings.dc_target_import_kw.get(site_id or ""),
                    scheduled_kw=committed,
                    committed_kw=committed,
                    available_kw=available,
                ),
                params,
                self._dc_states.get(oid, dc.DataCenterState(output_kw=committed)),
                now_s=now.timestamp(),
                dt_s=dt_s,
            )
            self._dc_states[oid] = out.state
            self._note_mode(oid, out.mode.value, out.reason_code)
            return out.setpoint_kw
        if spec.service_type == PIPELINE_AC_SERVICE_TYPE:
            defaults = self._settings.pipeline_ac
            params_pac = pac.PipelineAcParams(
                line_kv=defaults.line_kv,
                power_factor=defaults.power_factor,
                shift_factor=defaults.shift_factor,
                direction=1 if defaults.direction >= 0 else -1,
                band_kw=committed,
                freshness_s=defaults.freshness_s,
            )
            out_pac = pac.step(
                pac.PipelineAcInputs(
                    current=self._feedback(spec, defaults.freshness_s, now),
                    scheduled_kw=committed,
                    committed_kw=committed,
                    available_kw=available,
                ),
                params_pac,
                self._pac_states.get(oid, pac.PipelineAcState()),
                dt_s=dt_s,
            )
            self._pac_states[oid] = out_pac.state
            self._note_mode(oid, out_pac.mode.value, out_pac.reason_code)
            return out_pac.setpoint_kw
        return None

    def _dc_params(self, committed: float) -> dc.DataCenterParams:
        base = max(committed, 1e-3)
        if self._dc_profile is not None:
            return dc.params_from_profile(self._dc_profile, base)
        return dc.DataCenterParams(deadband_kw=0.01 * base, ramp_kw_per_min=30.0 * base, freshness_s=2.0)

    def _note_mode(self, oid: str, mode: str, reason: str) -> None:
        if self._modes.get(oid) != mode:
            self._modes[oid] = mode
            self.mode_changes.append(ModeChange(oid, mode, reason))

    def reconcile(self, result: CycleResult) -> None:
        """Each controller learns what the cycle actually granted its obligation (a later HOLD holds it)."""
        delivered: dict[str, float] = {}
        for grant in result.grants:
            if grant.obligation_id is not None and not grant.is_headroom:
                delivered[grant.obligation_id] = delivered.get(grant.obligation_id, 0.0) + grant.granted_kw
        for oid, state in self._dc_states.items():
            self._dc_states[oid] = dc.with_delivered(state, delivered.get(oid, 0.0))
        for oid, state_pac in self._pac_states.items():
            self._pac_states[oid] = pac.with_delivered(state_pac, delivered.get(oid, 0.0))

    def measurements(self, now: datetime) -> dict[str, tuple[PqMeasurement | None, PqEnvelopeLimits]]:
        """S5.4 input per monitored obligation: its PCC measurement (`None` = missing or stale, never
        compliant) and its envelope. Obligations without an envelope are not monitored here."""
        out: dict[str, tuple[PqMeasurement | None, PqEnvelopeLimits]] = {}
        for oid, spec in self._specs.items():
            if spec.limits is None:
                continue
            source = _source_id(spec.feedback_signal_ref)
            if spec.service_type == DATA_CENTER_SERVICE_TYPE:
                reading = self._signals.latest_site(spec.customer_id, source) if source else None
                meter = self._feedback(spec, self._dc_params(1.0).freshness_s, now)
                out[oid] = (
                    dc.usable_pcc_measurement(reading, meter, nominal_v=self._settings.site_nominal_v),
                    spec.limits,
                )
            else:
                corridor = self._signals.latest_corridor(spec.customer_id, source) if source else None
                signal = self._feedback(spec, self._settings.pipeline_ac.freshness_s, now)
                if corridor is None or signal is None or not signal.usable:
                    out[oid] = (None, spec.limits)
                else:
                    out[oid] = (
                        pac.corridor_measurement(corridor),
                        pac.corridor_limits(spec.limits, corridor),
                    )
        return out


def _source_id(ref: str | None) -> str | None:
    if not ref:
        return None
    try:
        return parse_feedback_ref(ref).source_id
    except FeedbackRefError:
        return None
