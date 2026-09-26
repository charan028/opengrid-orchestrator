"""opengrid.assets.runner -- periodic asset-health drift-evaluation sweep (07-delivery/06 S5.5.1-4,
WP-I). Closes the gap flagged in the WP-I build report: nothing was calling `AssetHealthService`, so
`asset_state` stayed `OK` and no work orders/asset events were ever written.

**Host process: `opengrid.settle`** (evidence, not a guess -- see the build report for the full
comparison). `settle`'s `main.py` already runs a `JobRunner` of independent `(name, Cadence, coroutine)`
jobs (heartbeat, `health.evaluate_once`, `settle_job`, `trace_prune`), each its own `asyncio.Task`, with
its own Postgres pool and NO MQTT client -- exactly this sweep's shape (read Postgres, call
`AssetHealthService`, write Postgres/trace; the resulting `CalibrationCommand` is signed and published by
the guardian process, not here). `opengrid.engine` was rejected: it already owns the 2s allocator tick,
its own MQTT ingest loop (`tel/#`, `ack/#`, `scada/#`) and the 15-min gate scheduler, and its own
comments document cycle-latency as a live incident source -- the process least able to absorb one more
per-tick concern. See this package's README for the exact `settle/main.py` wiring lines.

`run_once()` is the single entry point a host process's job scheduler calls on its own cadence (default
60s, matching `settle`'s other job cadences) -- it never owns a loop or a clock itself (BUILD.md S5a "no
flaky sleeps: use injected clocks"; the caller's scheduler is the clock).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from opengrid.assets.calibration import CalibrationReference
from opengrid.assets.service import AssetHealthService
from opengrid.core.pq import DEFAULT_FIRMWARE_CALIBRATION_BOUNDS, CalibrationBounds
from opengrid.core.pq.constants import NOMINAL_FREQ_HZ
from opengrid.pq_ingest.aggregation import NOMINAL_VOLTAGE_V

logger = logging.getLogger(__name__)

#: S6.7: "bounds are configured per inverter model/firmware family". The single canonical default in
#: `opengrid.core.pq`, the same one guardian's `StaticFirmwareCalibrationBoundsPort` uses, so the ladder's
#: PRIMARY bound choice and the guardian's INDEPENDENT re-check (G-25) cannot drift apart.
DEFAULT_CALIBRATION_BOUNDS = DEFAULT_FIRMWARE_CALIBRATION_BOUNDS

DEFAULT_CALIBRATION_LEASE_TTL_S = 30.0

#: The severity every drift-driven work order opens at. An operator reviewing `og.maintenance_work_
#: order` retriages from evidence; the sweep itself has no basis to distinguish LOW/MEDIUM/URGENT.
DEFAULT_WORK_ORDER_SEVERITY = "HIGH"

#: SWEEP DISABLED (R2 incident, 2026-09-26): `PgDriftObservationRepo` fed the OLDEST summary in the
#: window as "the latest measured offset" (rows come back newest-first; see `repo.py`'s "LIVE BUG FIX"
#: comment) -- hub-01996 measured 0.017 Hz against a real 0.2 Hz drift, the resulting correction was
#: wrong, the post-check read `WORSE_ROLLED_BACK`, and the hub was quarantined. The same ordering bug
#: produced 161 `NO_CHANGE` outcomes fleet-wide -- false positives, not real hardware faults. The
#: ordering bug itself is fixed (`repo.py`), but the sweep stays OFF until the owner or lead explicitly
#: re-enables it (`enabled=True`) -- do not flip this default without that sign-off, and prefer passing
#: `min_consecutive_exceedances=PROPOSED_MIN_CONSECUTIVE_EXCEEDANCES` (`service.py`) when it is re-enabled.
DRIFT_SWEEP_ENABLED_DEFAULT = False


@dataclass(frozen=True, slots=True)
class RunOnceResult:
    """One sweep's summary, for logging/metrics. Never raised on a single hub's failure (K7: one bad
    hub must not stop the whole sweep) -- `errors` counts how many were skipped."""

    evaluated: int
    calibrations_requested: int
    work_orders_opened: int
    errors: int


async def run_once(
    service: AssetHealthService,
    *,
    now: datetime,
    bounds: CalibrationBounds = DEFAULT_CALIBRATION_BOUNDS,
    lease_ttl_s: float = DEFAULT_CALIBRATION_LEASE_TTL_S,
    enabled: bool = DRIFT_SWEEP_ENABLED_DEFAULT,
    min_consecutive_exceedances: int | None = None,
) -> RunOnceResult:
    """One drift-evaluation sweep over every hub with a characterization row (`og.hub_inverter_pq`):
    `evaluate_drift` -> (`WATCH`: `request_calibration`, S5.4 step 3/S5.5.4) -> (`DEGRADED` with no open
    work order yet: `open_work_order`, S5.5.5). Verification and re-admission/further escalation happen
    asynchronously as `CalibrationAck`s arrive (`opengrid.assets.calibration_ack.handle_calibration_
    ack`), not in this sweep -- `request_calibration` only builds and durably records the candidate
    (`og.calibration_attempt`, outcome `PENDING`) for the guardian to sign; it never publishes anything
    itself (K3: only the guardian signs).

    `enabled` defaults to `DRIFT_SWEEP_ENABLED_DEFAULT` (currently `False` -- see that constant's own
    docstring, R2 incident 2026-09-26): a caller relying on the default gets a safe, immediate no-op
    (logged once) rather than the sweep silently resuming the moment this module is redeployed. Passing
    `enabled=True` explicitly is how the owner/lead re-enables it once ready."""
    if not enabled:
        logger.warning(
            "asset drift sweep is disabled (DRIFT_SWEEP_ENABLED_DEFAULT=False, R2 incident 2026-09-26); "
            "run_once() is a no-op until re-enabled with enabled=True"
        )
        return RunOnceResult(evaluated=0, calibrations_requested=0, work_orders_opened=0, errors=0)

    hub_ids = await service.ports.asset_health.list_hub_ids()
    evaluated = requested = opened = errors = 0

    for hub_id in hub_ids:
        try:
            state = await service.evaluate_drift(
                hub_id, now=now, min_consecutive_exceedances=min_consecutive_exceedances
            )
        except Exception:
            errors += 1
            logger.exception("asset drift evaluation failed", extra={"hub_id": hub_id})
            continue
        evaluated += 1
        if state is None:
            continue

        try:
            if state == "WATCH" and await _maybe_request_calibration(
                service, hub_id, now=now, bounds=bounds, lease_ttl_s=lease_ttl_s
            ):
                requested += 1
            elif state == "DEGRADED" and await _maybe_open_work_order(service, hub_id, now=now):
                opened += 1
        except Exception:
            errors += 1
            logger.exception("asset drift follow-up action failed", extra={"hub_id": hub_id, "state": state})

    return RunOnceResult(
        evaluated=evaluated, calibrations_requested=requested, work_orders_opened=opened, errors=errors
    )


async def _maybe_request_calibration(
    service: AssetHealthService,
    hub_id: str,
    *,
    now: datetime,
    bounds: CalibrationBounds,
    lease_ttl_s: float,
) -> bool:
    """Builds the grid-synchronized reference from the fleet's own nominal constants (S5.5.4's reference
    phase/frequency/amplitude) -- `request_calibration` itself refuses (returns `None`, no candidate
    built) when a sensitive grant is still active or the 24h rate limit has not cleared, so this sweep
    never needs to duplicate either check."""
    reference = CalibrationReference(
        phase_deg=0.0, freq_hz=NOMINAL_FREQ_HZ, amplitude_v=NOMINAL_VOLTAGE_V, sync_source="ntp_disciplined"
    )
    # No epoch/seq here: the guardian assigns the real per-hub (epoch, seq) on `og.calibration_command`
    # (migration 0016) when it signs; the sweep never writes a placeholder (#30).
    candidate = await service.request_calibration(
        hub_id,
        reference=reference,
        bounds=bounds,
        now=now,
        lease_ttl_s=lease_ttl_s,
    )
    return candidate is not None


async def _maybe_open_work_order(service: AssetHealthService, hub_id: str, *, now: datetime) -> bool:
    existing = await service.ports.work_orders.open_for_hub(hub_id)
    if existing is not None:
        return False
    await service.open_work_order(
        hub_id,
        severity=DEFAULT_WORK_ORDER_SEVERITY,
        evidence={"reason": "asset_drift_sweep_degraded", "detected_at": now.isoformat()},
        now=now,
    )
    return True
