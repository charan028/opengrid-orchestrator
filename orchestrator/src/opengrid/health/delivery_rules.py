"""The measured-delivery alert rules (D-38), raised and cleared by og-settle's delivery job
(`opengrid.delivery.job`), which owns them; health never auto-clears them. Pure: the job passes the
call's facts in. One place names each rule, severity, summary and condition key (BUILD.md S1).

- ALR-DELIVERY-RAMP-LATE: the call has run past its product ramp time and delivery never reached target;
- ALR-DELIVERY-SHORTFALL: after the ramp, delivery has been below target for longer than N s;
- ALR-DELIVERY-NONE: signed commands ask for discharge but no measurable delivery for longer than N s;
- ALR-DELIVERY-METER-MISMATCH: at the end of a call the independent meter disagrees with battery
  telemetry beyond tolerance (the record is UNCORROBORATED). Cleared when the same meter agrees again on a
  later call, or by an operator (`POST /og/api/delivery/records/{call_id}/meter-mismatch/clear`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from opengrid.health.model import AlertFinding

ALR_DELIVERY_RAMP_LATE = "ALR-DELIVERY-RAMP-LATE"
ALR_DELIVERY_SHORTFALL = "ALR-DELIVERY-SHORTFALL"
ALR_DELIVERY_NONE = "ALR-DELIVERY-NONE"
ALR_DELIVERY_METER_MISMATCH = "ALR-DELIVERY-METER-MISMATCH"
DELIVERY_ALERT_RULES = frozenset(
    {ALR_DELIVERY_RAMP_LATE, ALR_DELIVERY_SHORTFALL, ALR_DELIVERY_NONE, ALR_DELIVERY_METER_MISMATCH}
)
#: The live rules that mean the obligation is being delivered short now (AT_RISK while open).
SHORT_RULES = frozenset({ALR_DELIVERY_SHORTFALL, ALR_DELIVERY_NONE})


@dataclass(frozen=True, slots=True)
class DeliveryAlertFacts:
    """One call's live facts (kW signed +charge/-discharge)."""

    call_id: str
    call_kind: str
    obligation_id: UUID | None
    utility_id: str | None
    committed_kw: float
    delivered_kw: float | None
    active: bool
    elapsed_s: float
    ramp_time_s: float
    reached_target: bool
    current_below_s: float
    shortfall_alert_s: float
    commanded_without_delivery_s: float
    none_alert_s: float


def _detail(facts: DeliveryAlertFacts, **extra: object) -> dict[str, object]:
    return {
        "call_id": facts.call_id,
        "call_kind": facts.call_kind,
        "obligation_id": str(facts.obligation_id) if facts.obligation_id else None,
        "utility_id": facts.utility_id,
        "committed_kw": facts.committed_kw,
        "delivered_kw": facts.delivered_kw,
        "scope_kind": "DELIVERY_CALL",
        "scope_ref": facts.call_id,
        **extra,
    }


def _finding(rule: str, facts: DeliveryAlertFacts, summary: str, **extra: object) -> AlertFinding:
    return AlertFinding(
        rule=rule,
        severity="critical",
        summary=summary,
        condition_key=f"{rule}:{facts.call_id}",
        detail=_detail(facts, **extra),
    )


def evaluate_delivery_alerts(facts: DeliveryAlertFacts) -> list[AlertFinding]:
    """The live alerts that should be open for a running call (none once it has ended)."""
    if not facts.active:
        return []
    found: list[AlertFinding] = []
    delivered = f"{facts.delivered_kw:.0f}" if facts.delivered_kw is not None else "unknown"
    if not facts.reached_target and facts.elapsed_s >= facts.ramp_time_s:
        found.append(
            _finding(
                ALR_DELIVERY_RAMP_LATE,
                facts,
                f"Call {facts.call_id} not at target after {facts.elapsed_s:.0f} s "
                f"(ramp {facts.ramp_time_s:.0f} s): delivered {delivered} kW of {facts.committed_kw:.0f} kW",
                elapsed_s=facts.elapsed_s,
                ramp_time_s=facts.ramp_time_s,
            )
        )
    if facts.current_below_s >= facts.shortfall_alert_s:
        found.append(
            _finding(
                ALR_DELIVERY_SHORTFALL,
                facts,
                f"Call {facts.call_id} below target for {facts.current_below_s:.0f} s: delivered {delivered} kW "
                f"of {facts.committed_kw:.0f} kW",
                below_s=facts.current_below_s,
            )
        )
    if facts.commanded_without_delivery_s >= facts.none_alert_s:
        found.append(
            _finding(
                ALR_DELIVERY_NONE,
                facts,
                f"Call {facts.call_id} commanded for {facts.commanded_without_delivery_s:.0f} s with no "
                "measurable delivery",
                commanded_without_delivery_s=facts.commanded_without_delivery_s,
            )
        )
    return found


def evaluate_delivery_meter_mismatch_alert(
    facts: DeliveryAlertFacts,
    *,
    mismatch_frac: float | None,
    window_end: datetime,
    meter_bank_ids: list[str] | None = None,
) -> AlertFinding:
    """ALR-DELIVERY-METER-MISMATCH for a final record whose meter check is UNCORROBORATED. It clears when the
    same meter agrees with battery telemetry on a later call, or when an operator clears it
    (`POST /og/api/delivery/records/{call_id}/meter-mismatch/clear`, reason required, traced)."""
    pct = f"{100.0 * mismatch_frac:.0f}%" if mismatch_frac is not None else "unknown"
    return _finding(
        ALR_DELIVERY_METER_MISMATCH,
        facts,
        f"Call {facts.call_id}: independent meter disagrees with battery telemetry by {pct}; delivery "
        "UNCORROBORATED",
        mismatch_frac=mismatch_frac,
        window_end=window_end.isoformat(),
        meter_bank_ids=list(meter_bank_ids or []),
    )
