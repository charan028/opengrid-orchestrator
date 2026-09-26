"""Tests for every CUSTOMER_* anomaly type: applies while active, reverts on duration
elapse or on being consumed (one-shot types)."""

from __future__ import annotations

from ogsim.customer.anomalies import CUSTOMER_ANOMALY_TYPES, CustomerAnomalyManager

CUSTOMER_ID = "cust-1"


def _manager() -> CustomerAnomalyManager:
    return CustomerAnomalyManager([CUSTOMER_ID, "cust-2"])


def test_load_step_datacenter_applies_and_reverts():
    manager = _manager()
    manager.start("a1", "load_step_datacenter", CUSTOMER_ID, {"step_kw": 500.0}, 0.0, 10.0)
    assert manager.modifiers[CUSTOMER_ID].load_step_kw == 500.0
    manager.tick(11.0)
    assert manager.modifiers[CUSTOMER_ID].load_step_kw == 0.0


def test_load_step_datacenter_uses_default_when_no_param_given():
    manager = _manager()
    manager.start("a1", "load_step_datacenter", CUSTOMER_ID, {}, 0.0, 10.0)
    assert manager.modifiers[CUSTOMER_ID].load_step_kw == 500.0


def test_pipeline_current_surge_applies_and_reverts():
    manager = _manager()
    manager.start("a1", "pipeline_current_surge", CUSTOMER_ID, {"surge_a": 25.0}, 0.0, 10.0)
    assert manager.modifiers[CUSTOMER_ID].current_surge_a == 25.0
    manager.tick(11.0)
    assert manager.modifiers[CUSTOMER_ID].current_surge_a == 0.0


def test_site_meter_stale_applies_and_reverts():
    manager = _manager()
    manager.start("a1", "site_meter_stale", CUSTOMER_ID, {}, 0.0, 10.0)
    assert manager.modifiers[CUSTOMER_ID].meter_stale is True
    manager.tick(11.0)
    assert manager.modifiers[CUSTOMER_ID].meter_stale is False


def test_request_burst_is_pending_then_consumed_once():
    manager = _manager()
    manager.start("a1", "request_burst", CUSTOMER_ID, {"count": 7}, 0.0, 1.0)
    assert manager.take_pending_request_burst(CUSTOMER_ID) == 7
    assert manager.take_pending_request_burst(CUSTOMER_ID) == 0


def test_request_burst_default_count():
    manager = _manager()
    manager.start("a1", "request_burst", CUSTOMER_ID, {}, 0.0, 1.0)
    assert manager.take_pending_request_burst(CUSTOMER_ID) == 5


def test_malformed_request_is_pending_then_consumed_once():
    manager = _manager()
    manager.start("a1", "malformed_request", CUSTOMER_ID, {}, 0.0, 1.0)
    assert manager.take_pending_malformed_request(CUSTOMER_ID) is True
    assert manager.take_pending_malformed_request(CUSTOMER_ID) is False


def test_late_cancellation_is_pending_then_consumed_once():
    manager = _manager()
    manager.start("a1", "late_cancellation", CUSTOMER_ID, {}, 0.0, 1.0)
    assert manager.take_pending_late_cancellation(CUSTOMER_ID) is True
    assert manager.take_pending_late_cancellation(CUSTOMER_ID) is False


def test_invoice_dispute_is_pending_with_reason_then_consumed_once():
    manager = _manager()
    manager.start("a1", "invoice_dispute", CUSTOMER_ID, {"reason_code": "BAD_METER"}, 0.0, 1.0)
    assert manager.take_pending_invoice_dispute(CUSTOMER_ID) == "BAD_METER"
    assert manager.take_pending_invoice_dispute(CUSTOMER_ID) is None


def test_invoice_dispute_default_reason_code():
    manager = _manager()
    manager.start("a1", "invoice_dispute", CUSTOMER_ID, {}, 0.0, 1.0)
    assert manager.take_pending_invoice_dispute(CUSTOMER_ID) == "USAGE_MISMATCH"


def test_a_one_shot_anomaly_left_unconsumed_still_clears_on_expiry():
    manager = _manager()
    manager.start("a1", "malformed_request", CUSTOMER_ID, {}, 0.0, 5.0)
    manager.tick(6.0)
    assert manager.modifiers[CUSTOMER_ID].pending_malformed_request is False


def test_wildcard_target_applies_to_every_customer():
    manager = _manager()
    manager.start("a1", "load_step_datacenter", "*", {"step_kw": 100.0}, 0.0, 10.0)
    assert manager.modifiers[CUSTOMER_ID].load_step_kw == 100.0
    assert manager.modifiers["cust-2"].load_step_kw == 100.0


def test_unknown_target_ref_applies_to_no_customer():
    manager = _manager()
    manager.start("a1", "load_step_datacenter", "not-a-customer", {"step_kw": 100.0}, 0.0, 10.0)
    assert manager.modifiers[CUSTOMER_ID].load_step_kw == 0.0
    assert manager.modifiers["cust-2"].load_step_kw == 0.0


def test_large_load_curtailment_request_mirrors_load_step_datacenter():
    """Build phase, 2026-09-26 (svc-large-load.yaml): the catalogue documents
    `large_load_curtailment_request` as a 1:1 mirror of `load_step_datacenter`'s wire shape, so it
    shares the same `load_step_kw` modifier -- both the start and the expiry/clear paths."""
    manager = _manager()
    manager.start("a1", "large_load_curtailment_request", CUSTOMER_ID, {"step_kw": 2000.0}, 0.0, 10.0)
    assert manager.modifiers[CUSTOMER_ID].load_step_kw == 2000.0
    manager.tick(11.0)
    assert manager.modifiers[CUSTOMER_ID].load_step_kw == 0.0


def test_every_catalogue_type_is_covered_by_this_test_module():
    covered = {
        "load_step_datacenter",
        "pipeline_current_surge",
        "site_meter_stale",
        "request_burst",
        "malformed_request",
        "late_cancellation",
        "invoice_dispute",
        "large_load_curtailment_request",
    }
    assert covered == CUSTOMER_ANOMALY_TYPES
