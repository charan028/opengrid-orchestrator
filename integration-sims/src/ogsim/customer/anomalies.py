"""ogsim.customer.anomalies -- CUSTOMER_* anomaly catalogue application/reversion.

Driven by `<root>/scenario/cmd` (parsed by `ogsim.common.scenario.parse_scenario_cmd`).
Timed anomalies (`load_step_datacenter`, `pipeline_current_surge`, `site_meter_stale`) hold
a per-customer modifier while active; one-shot anomalies (`request_burst`,
`malformed_request`, `late_cancellation`, `invoice_dispute`) queue a single pending action
`ogsim.customer.runtime` consumes and clears on its next tick, mirroring
`ogsim.scada.anomalies`' `pending_instruction` pattern.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CUSTOMER_ANOMALY_TYPES = frozenset(
    {
        "load_step_datacenter",
        "pipeline_current_surge",
        "request_burst",
        "malformed_request",
        "late_cancellation",
        "invoice_dispute",
        "site_meter_stale",
        # LARGE_LOAD's own curtailment/ride-through load step (build phase, 2026-09-26,
        # svc-large-load.yaml): the catalogue documents it as a 1:1 mirror of
        # `load_step_datacenter`'s wire shape (a generic CUSTOMER load step at a site meter), so it
        # shares that same `load_step_kw` modifier below rather than a separate one.
        "large_load_curtailment_request",
    }
)


@dataclass
class ActiveCustomerAnomaly:
    id: str
    type: str
    customer_ids: list[str]
    params: dict[str, Any]
    start: float
    duration: float | None

    def is_active_at(self, now: float) -> bool:
        if now < self.start:
            return False
        return self.duration is None or now < self.start + self.duration


@dataclass
class CustomerModifiers:
    load_step_kw: float = 0.0
    current_surge_a: float = 0.0
    meter_stale: bool = False
    pending_request_burst: int = 0
    pending_malformed_request: bool = False
    pending_late_cancellation: bool = False
    pending_invoice_dispute: str | None = None


class CustomerAnomalyManager:
    """Applies/reverts CUSTOMER_* anomalies against per-customer `CustomerModifiers`."""

    def __init__(self, customer_ids: list[str]) -> None:
        self.customer_ids = customer_ids
        self.modifiers: dict[str, CustomerModifiers] = {c: CustomerModifiers() for c in customer_ids}
        self._active: dict[str, ActiveCustomerAnomaly] = {}

    def _resolve_targets(self, target_ref: str) -> list[str]:
        if target_ref in ("*", ""):
            return list(self.customer_ids)
        return [target_ref] if target_ref in self.modifiers else []

    def start(
        self,
        anomaly_id: str,
        anomaly_type: str,
        target_ref: str,
        params: dict[str, Any],
        start: float,
        duration_s: float | None,
    ) -> ActiveCustomerAnomaly:
        customers = self._resolve_targets(target_ref)
        anomaly = ActiveCustomerAnomaly(anomaly_id, anomaly_type, customers, params, start, duration_s)
        self._active[anomaly_id] = anomaly
        self._apply(anomaly)
        return anomaly

    def tick(self, now: float) -> None:
        expired = [a for a in self._active.values() if not a.is_active_at(now)]
        for anomaly in expired:
            self._revert(anomaly)
            del self._active[anomaly.id]

    def _apply(self, anomaly: ActiveCustomerAnomaly) -> None:
        for customer_id in anomaly.customer_ids:
            m = self.modifiers[customer_id]
            p = anomaly.params
            if anomaly.type in ("load_step_datacenter", "large_load_curtailment_request"):
                m.load_step_kw = float(p.get("step_kw", 500.0))
            elif anomaly.type == "pipeline_current_surge":
                m.current_surge_a = float(p.get("surge_a", 20.0))
            elif anomaly.type == "site_meter_stale":
                m.meter_stale = True
            elif anomaly.type == "request_burst":
                m.pending_request_burst = int(p.get("count", 5))
            elif anomaly.type == "malformed_request":
                m.pending_malformed_request = True
            elif anomaly.type == "late_cancellation":
                m.pending_late_cancellation = True
            elif anomaly.type == "invoice_dispute":
                m.pending_invoice_dispute = str(p.get("reason_code", "USAGE_MISMATCH"))

    def _revert(self, anomaly: ActiveCustomerAnomaly) -> None:
        for customer_id in anomaly.customer_ids:
            m = self.modifiers[customer_id]
            if anomaly.type in ("load_step_datacenter", "large_load_curtailment_request"):
                m.load_step_kw = 0.0
            elif anomaly.type == "pipeline_current_surge":
                m.current_surge_a = 0.0
            elif anomaly.type == "site_meter_stale":
                m.meter_stale = False
            # One-shot types normally self-clear when `runtime` consumes their pending_*
            # field (the take_pending_* methods below); a duration elapsing before that
            # happens still clears them here defensively so nothing lingers forever.
            elif anomaly.type == "request_burst":
                m.pending_request_burst = 0
            elif anomaly.type == "malformed_request":
                m.pending_malformed_request = False
            elif anomaly.type == "late_cancellation":
                m.pending_late_cancellation = False
            elif anomaly.type == "invoice_dispute":
                m.pending_invoice_dispute = None

    def take_pending_request_burst(self, customer_id: str) -> int:
        m = self.modifiers[customer_id]
        count = m.pending_request_burst
        m.pending_request_burst = 0
        return count

    def take_pending_malformed_request(self, customer_id: str) -> bool:
        m = self.modifiers[customer_id]
        flag = m.pending_malformed_request
        m.pending_malformed_request = False
        return flag

    def take_pending_late_cancellation(self, customer_id: str) -> bool:
        m = self.modifiers[customer_id]
        flag = m.pending_late_cancellation
        m.pending_late_cancellation = False
        return flag

    def take_pending_invoice_dispute(self, customer_id: str) -> str | None:
        m = self.modifiers[customer_id]
        reason = m.pending_invoice_dispute
        m.pending_invoice_dispute = None
        return reason


__all__ = ["CUSTOMER_ANOMALY_TYPES", "ActiveCustomerAnomaly", "CustomerAnomalyManager", "CustomerModifiers"]
