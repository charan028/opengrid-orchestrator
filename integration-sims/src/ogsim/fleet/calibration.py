"""ogsim.fleet.calibration -- CalibrationCommand verification and application.

Implements `06-service-profiles-and-power-quality.md` §5.5.4 (the remote-
calibration ladder step) and §6.7 (the `CalibrationCommand`/`CalibrationAck`
wire shapes) on ogsim's own inverter model (`ogsim.fleet.pq`). Pure logic, no
MQTT -- mirrors `ogsim.fleet.commands`'s shape (signature then freshness,
independent of each other; a `CalibrationOutcome` per hub the caller acks),
and reuses its RFC3339 parsing rather than duplicating it.

A `calibration_drift_correctable` anomaly's drift is fully or partially
removed in proportion to the command's `correction` fields; a
`calibration_drift_hardware` anomaly is never affected by any command,
exercising the escalation path (§5.5.4) deterministically in tests.

Freshness (K6 pattern): the guardian assigns each hub's calibration commands a
strictly increasing `(epoch, seq)`. A hub rejects (`STALE_SEQ`) any command whose
pair does not exceed the last one it APPLIED, so a captured command can never be
replayed within its lease, even across the 24 h rate-limit window.

Rollback is hub-local (§5.5.4 "automatic rollback"): when applying a command makes
a unit worse, the hub itself restores the parameters it had immediately before that
same command, inside the same apply, and acks `WORSE_ROLLED_BACK`. No second remote
command is ever needed or sent for a rollback, so the guardian's G-25 rate limit
(one signed calibration command per hub per 24 h) applies to every command without
exception, and a rollback cannot be used to chase the limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ogsim.common.crypto import verify_signature
from ogsim.fleet.commands import parse_rfc3339, utc_now_from_epoch
from ogsim.fleet.pq import InverterPqState, PqAnomalyManager

_SIGNED_FIELDS = (
    "calibration_id",
    "hub_id",
    "epoch",
    "seq",
    "issued_at",
    "expires_at",
    "reference",
    "correction",
    "bounds",
)
_CORRECTION_TO_BOUND = {
    "freq_hz": "max_freq_hz",
    "voltage_pct": "max_voltage_pct",
    "phase_deg": "max_phase_deg",
}
_CORRECTION_TO_OFFSET_FIELD = {
    "freq_hz": ("freq_offset_hz", "freq_offset_baseline_hz"),
    "voltage_pct": ("voltage_offset_pct", "voltage_offset_baseline_pct"),
    "phase_deg": ("phase_angle_error_deg", "phase_angle_error_baseline_deg"),
}
_RESIDUAL_TOLERANCE = 1e-9


@dataclass(frozen=True)
class CalibrationOutcome:
    calibration_id: str
    hub_id: str
    applied: bool
    status: str  # APPLIED | REJECTED | EXPIRED
    outcome_label: str | None  # CORRECTED | IMPROVED | NO_CHANGE | WORSE_ROLLED_BACK | None
    resulting_offsets: dict[str, float] | None
    reject_reason: str | None


def _signing_fields(command: dict[str, Any]) -> dict[str, Any]:
    return {k: command[k] for k in _SIGNED_FIELDS if k in command}


def verify_calibration_signature(command: dict[str, Any], public_key: Ed25519PublicKey) -> bool:
    """Verifies the command envelope's Ed25519 signature (crypto.md §2.1 pattern)."""
    return verify_signature(public_key, _signing_fields(command), command.get("signature"))


def _bounds_violation(correction: dict[str, Any], bounds: dict[str, Any]) -> bool:
    for field_name, bound_name in _CORRECTION_TO_BOUND.items():
        value = correction.get(field_name)
        if value is None:
            continue
        limit = bounds.get(bound_name)
        if limit is not None and abs(float(value)) > float(limit):
            return True
    return False


def _reject(calibration_id: str, hub_id: str, reason: str) -> CalibrationOutcome:
    return CalibrationOutcome(calibration_id, hub_id, False, "REJECTED", None, None, reason)


def apply_calibration(
    pq: InverterPqState,
    anomalies: PqAnomalyManager,
    command: dict[str, Any],
    public_key: Ed25519PublicKey,
    now: float,
    rate_limit_s: float,
) -> CalibrationOutcome:
    """Verifies and applies one `CalibrationCommand` against `hub_id`'s
    unit(s). Never raises on a malformed/unauthorized command -- an
    unverifiable or out-of-bounds command is reported REJECTED, exactly as
    G-25 (guardian check, orchestrator-side) would refuse to sign it; this
    module only mirrors that discipline on the hub side."""
    calibration_id = str(command.get("calibration_id", ""))
    hub_id = str(command.get("hub_id", ""))
    idx = pq.indices_for_hub(hub_id)
    if not idx:
        return _reject(calibration_id, hub_id, "UNKNOWN_HUB")
    if not verify_calibration_signature(command, public_key):
        return _reject(calibration_id, hub_id, "BAD_SIGNATURE")

    issued_at = parse_rfc3339(command["issued_at"])
    expires_at = parse_rfc3339(command["expires_at"])
    now_dt = utc_now_from_epoch(now)
    if not (issued_at <= now_dt < expires_at):
        return CalibrationOutcome(calibration_id, hub_id, False, "EXPIRED", None, None, "EXPIRED")

    try:
        epoch, seq = int(command["epoch"]), int(command["seq"])
    except (KeyError, TypeError, ValueError):
        return _reject(calibration_id, hub_id, "STALE_SEQ")
    if any((epoch, seq) <= (int(pq.last_calibration_epoch[i]), int(pq.last_calibration_seq[i])) for i in idx):
        return _reject(calibration_id, hub_id, "STALE_SEQ")

    if any(pq.last_calibration_at[i] >= 0 and now - pq.last_calibration_at[i] < rate_limit_s for i in idx):
        return _reject(calibration_id, hub_id, "RATE_LIMITED")

    correction: dict[str, Any] = command.get("correction") or {}
    bounds: dict[str, Any] = command.get("bounds") or {}
    if _bounds_violation(correction, bounds):
        return _reject(calibration_id, hub_id, "BOUNDS_EXCEEDED")

    outcome_label, resulting = _apply_to_units(pq, anomalies, idx, correction)
    for i in idx:
        pq.last_calibration_at[i] = now
        pq.last_calibration_epoch[i] = epoch
        pq.last_calibration_seq[i] = seq
    return CalibrationOutcome(calibration_id, hub_id, True, "APPLIED", outcome_label, resulting, None)


def _apply_to_units(
    pq: InverterPqState, anomalies: PqAnomalyManager, idx: list[int], correction: dict[str, Any]
) -> tuple[str, dict[str, float]]:
    """Applies `correction` to every unit in `idx`, returns the worst-case
    outcome label across units and the resulting offsets of the first unit
    (a `CalibrationCommand` targets one hub; its one or two units normally
    agree closely enough that reporting one is representative)."""
    labels = [_apply_to_one_unit(pq, anomalies, i, correction) for i in idx]
    outcome_label = _worst_outcome(labels)
    first = idx[0]
    resulting = {
        "freq_hz": float(pq.freq_offset_hz[first]),
        "voltage_pct": float(pq.voltage_offset_pct[first]),
        "phase_deg": float(pq.phase_angle_error_deg[first]),
    }
    return outcome_label, resulting


_OUTCOME_SEVERITY = {"CORRECTED": 0, "IMPROVED": 1, "NO_CHANGE": 2, "WORSE_ROLLED_BACK": 3}


def _worst_outcome(labels: list[str]) -> str:
    return max(labels, key=lambda label: _OUTCOME_SEVERITY[label])


def _apply_to_one_unit(
    pq: InverterPqState, anomalies: PqAnomalyManager, unit_index: int, correction: dict[str, Any]
) -> str:
    correctable = anomalies.drift_correctable[unit_index]
    pre_residual = _residual(pq, unit_index)
    if correctable is False:
        return "NO_CHANGE"  # calibration_drift_hardware: no command can move it.

    pre_values = {
        field_name: (getattr(pq, offset_field)[unit_index], getattr(pq, baseline_field)[unit_index])
        for field_name, (offset_field, baseline_field) in _CORRECTION_TO_OFFSET_FIELD.items()
    }
    for field_name, (offset_field, baseline_field) in _CORRECTION_TO_OFFSET_FIELD.items():
        magnitude = abs(float(correction.get(field_name, 0.0)))
        if magnitude == 0.0:
            continue
        offsets = getattr(pq, offset_field)
        baseline = getattr(pq, baseline_field)[unit_index]
        gap = offsets[unit_index] - baseline
        step = min(magnitude, abs(gap))
        offsets[unit_index] -= np_sign(gap) * step

    post_residual = _residual(pq, unit_index)
    if post_residual > pre_residual + _RESIDUAL_TOLERANCE:
        for field_name, (offset_field, _baseline_field) in _CORRECTION_TO_OFFSET_FIELD.items():
            getattr(pq, offset_field)[unit_index] = pre_values[field_name][0]
        return "WORSE_ROLLED_BACK"
    if post_residual <= _RESIDUAL_TOLERANCE:
        return "CORRECTED"
    if post_residual < pre_residual - _RESIDUAL_TOLERANCE:
        return "IMPROVED"
    return "NO_CHANGE"


def _residual(pq: InverterPqState, unit_index: int) -> float:
    return float(
        sum(
            abs(getattr(pq, offset_field)[unit_index] - getattr(pq, baseline_field)[unit_index])
            for offset_field, baseline_field in _CORRECTION_TO_OFFSET_FIELD.values()
        )
    )


def np_sign(value: float) -> float:
    """`math.copysign`-free sign helper: 0.0 for an exact-zero gap, so a
    correction never nudges an already-corrected axis off zero."""
    if value > 0.0:
        return 1.0
    if value < 0.0:
        return -1.0
    return 0.0


def current_offsets(pq: InverterPqState, hub_id: str) -> dict[str, float]:
    """The hub's present offsets (first unit), or zeros for an unknown hub: what a REJECTED/EXPIRED ack
    reports, since nothing was applied (the schema requires `resulting_offsets` on every ack)."""
    idx = pq.indices_for_hub(hub_id)
    if not idx:
        return {"freq_hz": 0.0, "voltage_pct": 0.0, "phase_deg": 0.0}
    first = idx[0]
    return {
        "freq_hz": float(pq.freq_offset_hz[first]),
        "voltage_pct": float(pq.voltage_offset_pct[first]),
        "phase_deg": float(pq.phase_angle_error_deg[first]),
    }


def build_calibration_ack(
    outcome: CalibrationOutcome,
    applied_at: str,
    *,
    command: dict[str, Any] | None = None,
    fallback_offsets: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Builds a `calibration_ack.schema.json`-shaped message (§6.7): it echoes the command's `(epoch,
    seq)` (the orchestrator binds the ack to the command it issued, crypto.md §2.5) and carries the
    hub's `reject_reason`, plus an internal `outcome` field (asset-health bookkeeping, §5.5.4) the
    MQTT publisher strips before sending."""
    ack: dict[str, Any] = {
        "calibration_id": outcome.calibration_id,
        "hub_id": outcome.hub_id,
        "applied": outcome.applied,
        "applied_at": applied_at,
        "resulting_offsets": outcome.resulting_offsets
        if outcome.resulting_offsets is not None
        else fallback_offsets,
        "status": outcome.status,
        "outcome": outcome.outcome_label,
        "reject_reason": outcome.reject_reason,
    }
    for field_name in ("epoch", "seq"):
        value = (command or {}).get(field_name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            ack[field_name] = value
    return ack
