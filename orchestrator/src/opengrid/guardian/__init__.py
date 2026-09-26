"""opengrid.guardian -- process og-guardian (02a S6). Owner: guardian agent (BUILD.md S4).

Runs G-01..G-06, G-09, G-13..G-15, G-19, G-20 (canonical, 00-invariants.md) using
`opengrid.core.limits`/`opengrid.core.timeutil` on its own independently-read inputs, then signs
(`opengrid.core.crypto`) or vetoes/times out.

The fixed public entry point is `evaluate_and_sign` (INTERFACES.md); it delegates to a `GuardianService`
configured once at process start-up via `configure()` -- `main.py` does this with real Postgres/MQTT-backed
ports (`opengrid.guardian.repo`/`mqtt_io`), tests construct a `GuardianService` directly with in-memory
fakes (see `opengrid.guardian.ports`) and either call it directly or through `configure()`.
"""

from __future__ import annotations

from opengrid.core.models.engine import CommandBatchRow, Verdict
from opengrid.core.models.pq import CalibrationCommand
from opengrid.guardian.pq_ports import ProposedCalibrationCommand
from opengrid.guardian.service import GuardianService

_service: GuardianService | None = None


def configure(service: GuardianService) -> None:
    """Install the `GuardianService` that `evaluate_and_sign` delegates to. Called once by `main.py`
    at start-up (or by a test that wants to exercise the module-level entry point)."""
    global _service
    _service = service


async def evaluate_and_sign(batch: CommandBatchRow) -> Verdict:
    """Run every applicable G-check against independently-read state; on PASS, sign the batch (K3: sole
    signer) and return outcome="PASS"; on any veto, return outcome in {"VETOED","PARTLY_VETOED"} with
    `.vetoed_rule_ids` populated; never signs unless the trace pre-image exists first (K10/G-14); a
    processing timeout or degraded clock quality (G-20) returns outcome="TIMEOUT" -- a hold, never a
    veto and never a stop (K7).
    """
    if _service is None:
        raise RuntimeError("opengrid.guardian.configure(service) must be called before evaluate_and_sign")
    return await _service.evaluate_and_sign(batch)


async def evaluate_and_sign_calibration(proposed: ProposedCalibrationCommand) -> CalibrationCommand | None:
    """S6.7/K14: run G-20 then G-25 on a candidate calibration command; on PASS return the signed
    `CalibrationCommand` (guardian-assigned per-hub `epoch`/`seq`), else `None` (a hold, traced).

    Cross-process callers do not call this: the ladder (`opengrid.assets`) records a PENDING
    `og.calibration_attempt` row, and `og-guardian` polls, evaluates and publishes it on
    `<root>/cmd/cal/<hub_id>` itself (`main.process_pending_calibrations`). The signing key never
    leaves the guardian process."""
    if _service is None:
        raise RuntimeError(
            "opengrid.guardian.configure(service) must be called before evaluate_and_sign_calibration"
        )
    return await _service.evaluate_and_sign_calibration(proposed)


__all__ = ["GuardianService", "configure", "evaluate_and_sign", "evaluate_and_sign_calibration"]
