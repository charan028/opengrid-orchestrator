"""Instantiation/round-trip tests for the pydantic contracts in opengrid.core.models.

These are shape tests (every field accepts its documented type and every wire model's
`signing_payload()` excludes the signature fields) rather than business-logic tests -- the modules
that build real rows/messages (feeds, selector, allocator, guardian, api) own behavioral coverage.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from opengrid.core.models import engine, mqtt, platform

NOW = datetime.now(UTC)


def test_telemetry_round_trip():
    msg = mqtt.Telemetry(
        hub_id="hub-1",
        bank_id="bank-1",
        zone="LZ_NORTH",
        ts=NOW,
        soc_kwh=5.0,
        p_kw=-1.0,
        health="online",
        seq=1,
        epoch=1,
    )
    assert msg.model_dump(mode="json")["health"] == "online"


def test_command_batch_signing_payload_excludes_signature_and_key_id():
    batch = mqtt.CommandBatch(
        batch_id=uuid4(),
        bank_id="bank-1",
        epoch=1,
        seq=1,
        issued_at=NOW,
        expires_at=NOW,
        items=[mqtt.CommandItem(hub_id="hub-1", p_kw_setpoint=-1.0, reason_code="SELECTOR")],
        key_id="guardian-2026a",
        signature="sig",
    )
    payload = batch.signing_payload()
    assert "signature" not in payload and "key_id" not in payload
    assert payload["bank_id"] == "bank-1"


def test_command_batch_signing_payload_is_exactly_the_spec_fields():
    """crypto.md S2.1 signs 7 fields; optional `precondition`/`lease` must not leak in (even as null),
    or the hub's recomputed signing input differs and every batch fails BAD_SIGNATURE."""
    batch = mqtt.CommandBatch(
        batch_id=uuid4(),
        bank_id="bank-1",
        epoch=1,
        seq=1,
        issued_at=NOW,
        expires_at=NOW,
        items=[mqtt.CommandItem(hub_id="hub-1", p_kw_setpoint=-1.0, reason_code="SELECTOR")],
        precondition=mqtt.CommandPrecondition(ledger_version=4),
        key_id="guardian-2026a",
        signature="sig",
    )
    assert tuple(sorted(batch.signing_payload())) == tuple(sorted(mqtt.COMMAND_BATCH_SIGNED_FIELDS))


def test_ack_reject_reason_optional():
    ack = mqtt.Ack(hub_id="hub-1", batch_id=uuid4(), accepted=False, reject_reason="STALE_SEQ", ts=NOW)
    assert ack.reject_reason == "STALE_SEQ"


def test_stop_event_signing_payload_excludes_signature_and_key_id():
    stop = mqtt.StopEvent(
        stop_id=uuid4(),
        scope="bank",
        scope_id="bank-1",
        action="ENGAGE",
        reason="overload",
        issued_by="SAFESTOP_AUTO",
        issued_at=NOW,
        key_id="safestop-2026a",
        signature="sig",
    )
    payload = stop.signing_payload()
    assert "signature" not in payload and "key_id" not in payload


def test_lease_and_scada_models():
    lease = mqtt.Lease(hub_id="hub-1", epoch=1, expires_at=NOW, issued_at=NOW)
    assert lease.epoch == 1
    signal = mqtt.ScadaBankSignal(
        bank_id="bank-1",
        signal="APPARENT_POWER_KVA",
        value=50.0,
        unit="kVA",
        quality="good",
        ts=NOW,
    )
    assert signal.quality == "good"
    instr = mqtt.ScadaUtilityInstruction(
        instruction_id=uuid4(),
        bank_id="bank-1",
        kind="LIMIT",
        limit_kw=10.0,
        issued_at=NOW,
        issued_by="utility",
    )
    assert instr.kind == "LIMIT"


def test_scenario_control_defaults():
    ctrl = mqtt.ScenarioControl(
        id=uuid4(),
        target=mqtt.ScenarioTarget(kind="bank", ref="bank-1"),
        type="SCADA_BANK_OVERLOAD",
        start=NOW,
    )
    assert ctrl.params == {}
    assert ctrl.duration_s is None


def test_contract_and_product_rule():
    contract = engine.Contract(
        contract_id=uuid4(),
        customer_id=uuid4(),
        service_type="ERCOT_AS",
        tier="T2",
        profile_ref="ercot-as-profile@1",
        start_at=NOW,
    )
    assert contract.status == "ACTIVE"
    rule = engine.ProductRule(
        product_rule_id=uuid4(),
        contract_id=contract.contract_id,
        product_code="NONSPIN",
        min_qty_kw=Decimal("100"),
        increment_kw=Decimal("100"),
        duration_minutes=60,
        variable_kind="SEMI_CONTINUOUS",
    )
    assert rule.block is False


def test_opportunity_obligation_commitment_chain():
    opp = engine.Opportunity(
        opportunity_id=uuid4(),
        contract_id=uuid4(),
        window_start=NOW,
        window_end=NOW,
        requested_kw=Decimal("10"),
        admitted_at=NOW,
    )
    assert opp.state == "OFFERED"

    obligation = engine.Obligation(
        obligation_id=uuid4(),
        opportunity_id=opp.opportunity_id,
        contract_id=opp.contract_id,
        service_type="ERCOT_AS",
        tier="T2",
        window_start=NOW,
        window_end=NOW,
        committed_qty_kw=Decimal("10"),
    )
    assert obligation.version == 1

    commitment = engine.Commitment(
        commitment_id=uuid4(),
        obligation_id=obligation.obligation_id,
        plan_id=uuid4(),
        interval_start=NOW,
        interval_end=NOW,
        committed_kw=Decimal("10"),
        variable_kind="SEMI_CONTINUOUS",
    )
    assert commitment.reason_code == "R-GATE-SELECT"


def test_plan_reservation_grant_command_batch_verdict_stop_event():
    plan = engine.Plan(
        plan_id=uuid4(),
        plan_mode="L-ID",
        gate_kind="SCHEDULED_15MIN",
        horizon_start=NOW,
        horizon_end=NOW,
        scenario_set=[{"scenario": "P50", "prob": 1.0}],
        solver_status="OPTIMAL",
    )
    assert plan.plan_mode == "L-ID"

    reservation = engine.Reservation(
        reservation_id=uuid4(),
        obligation_id=uuid4(),
        bank_id="bank-000",
        kind="POWER_KW",
        amount=Decimal("10"),
        interval_start=NOW,
        interval_end=NOW,
        ledger_version=1,
    )
    assert reservation.kind == "POWER_KW"

    grant = engine.Grant(
        grant_id=uuid4(),
        cycle_id="c1",
        bank_id="bank-000",
        granted_kw=Decimal("5"),
        ledger_version=1,
    )
    assert grant.is_headroom is False

    batch_row = engine.CommandBatchRow(
        command_batch_id=uuid4(),
        cycle_id="c1",
        ledger_version=1,
        submission_id="s1",
        command_count=1,
        merkle_root="abc",
    )
    assert batch_row.command_count == 1

    verdict = engine.Verdict(
        verdict_id=uuid4(),
        command_batch_id=batch_row.command_batch_id,
        outcome="PASS",
        latency_ms=10,
        inputs_hash="hash",
    )
    assert verdict.outcome == "PASS"

    stop_row = engine.StopEventRow(
        stop_event_id=uuid4(),
        scope_kind="BANK",
        scope_ref="bank-1",
        action="ENGAGE",
        initiator_kind="SAFESTOP_AUTHORITY",
        initiator_ref="safestop",
        reason="overload",
        signature="sig",
    )
    assert stop_row.scope_kind == "BANK"


def test_meter_performance_invoice_pnl_trace_retention_operator_action():
    meter = engine.MeterInterval(
        meter_interval_id=uuid4(),
        obligation_id=uuid4(),
        interval_start=NOW,
        interval_end=NOW,
        delivered_kwh=Decimal("1.5"),
        source="DIRECT_HUB_METER",
    )
    assert meter.quality_flag == "GOOD"

    perf = engine.Performance(
        performance_id=uuid4(),
        obligation_id=uuid4(),
        interval_start=NOW,
        interval_end=NOW,
        compliance_pct=Decimal("0.99"),
        passed_threshold=True,
    )
    assert perf.passed_threshold

    invoice = engine.InvoiceLine(
        invoice_line_id=uuid4(),
        contract_id=uuid4(),
        obligation_id=uuid4(),
        period_start=NOW.date(),
        period_end=NOW.date(),
        line_type="ENERGY",
        amount=Decimal("100"),
    )
    assert invoice.status == "PROVISIONAL"

    pnl = engine.Pnl(
        pnl_id=uuid4(),
        obligation_id=uuid4(),
        interval_start=NOW,
        interval_end=NOW,
        net_value=Decimal("10"),
    )
    assert pnl.revenue == Decimal("0")

    trace_row = engine.TraceRow(
        trace_id=uuid4(),
        decision_type="ADMISSION",
        event_class="ADMISSION",
        stream_id="s1",
        seq=0,
        payload={"a": 1},
        hash="h",
    )
    assert trace_row.seq == 0

    checkpoint = engine.TraceCheckpoint(
        checkpoint_id=uuid4(),
        checkpoint_at=NOW,
        stream_heads={"s1": {"seq": 0, "hash": "h"}},
        checkpoint_hash="ch",
    )
    assert checkpoint.checkpoint_hash == "ch"

    retention = engine.RetentionPolicy(event_class="SAFE_STOP", retention_days=3650)
    assert retention.prune_after_checkpoint is True

    op_action = engine.OperatorAction(
        operator_action_id=uuid4(),
        operator_ref="op-1",
        action_kind="APPROVAL",
    )
    assert op_action.action_kind == "APPROVAL"

    renom = engine.RenominationPoint(
        renomination_point_id=uuid4(),
        contract_id=uuid4(),
        scheduled_at=NOW,
    )
    assert renom.outcome is None


def test_platform_models():
    hub = platform.Hub(hub_id="hub-1", bank_id="bank-1", zone="LZ_NORTH", e_kwh=13.5, r_kwh=2.7, p_kw=5.0)
    assert hub.eta_c == 0.9487

    bank = platform.Bank(bank_id="bank-1", zone="LZ_NORTH", kva_rating=75.0)
    assert bank.reserve_kva == 0.0

    hub_state = platform.HubState(hub_id="hub-1", soc_kwh=5.0, p_kw=1.0, last_seen_at=NOW)
    assert hub_state.health == "online"

    heartbeat = platform.Heartbeat(process="og-engine", pid=1, ts=NOW)
    assert heartbeat.status == "ok"

    alert = platform.Alert(rule="feed-stale", severity="warning", summary="stale", opened_at=NOW)
    assert alert.severity == "warning"

    feed_obs = platform.FeedObs(
        source="ercot",
        product="np6-905-cd",
        series="LZ_NORTH",
        ts=NOW,
        value=42.0,
        unit="usd_per_mwh",
        quality="GOOD",
        recorded_at=NOW,
    )
    assert feed_obs.quality == "GOOD"

    feed_status = platform.FeedStatus(source="ercot", product="np6-905-cd")
    assert feed_status.breaker_open is False
