"""In-memory fakes for `opengrid.api`'s unit tests -- no Postgres involved (BUILD.md S5 "Local: unit
and property tests, no DB/MQTT"). Implements enough of `StoreProtocol`/`TraceBackend` to exercise every
router.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from opengrid.api.store import (
    AlertQuery,
    BankAggregate,
    FeedStatusRow,
    ForecastPoint,
    HealthSnapshot,
    ProcessHeartbeat,
)
from opengrid.core.models.engine import (
    CommandBatchRow,
    Commitment,
    Grant,
    InvoiceLine,
    Obligation,
    Opportunity,
    Performance,
    Plan,
    Reservation,
    TraceRow,
    Verdict,
)
from opengrid.core.models.platform import Alert, FeedObs
from opengrid.core.tracehash import ChainRecord

SAMPLE_HUB_ID = "hub-0001"
SAMPLE_BANK_ID = "bank-01"
SAMPLE_CONTRACT_ID = UUID("00000000-0000-7000-8000-000000000d02")
SAMPLE_CUSTOMER_ID = UUID("00000000-0000-7000-8000-000000000c02")


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class FakeStore:
    """Returns fixed, minimal-but-representative data for every read; records writes so tests can
    assert on them."""

    alerts: list[Alert] = field(default_factory=list)
    operator_actions: list[dict[str, Any]] = field(default_factory=list)
    retention: dict[str, int] = field(default_factory=lambda: {"OPERATOR_ACTION": 1825})
    command_batches: list[CommandBatchRow] = field(default_factory=list)
    verdicts: dict[UUID, Verdict] = field(default_factory=dict)
    #: Outcome `insert_command_batch` immediately stamps for the batch it just received, simulating an
    #: instant og-guardian response so tests don't wait out `_VERDICT_POLL_TIMEOUT_S`. Tests flip this
    #: (and `next_vetoed_rule_ids`) before confirming to exercise the veto/timeout paths.
    next_verdict_outcome: str = "PASS"
    next_vetoed_rule_ids: list[str] = field(default_factory=list)
    #: Simulates `og-safestop`'s own async response to the NOTIFY protocol: a PROPOSE is remembered by
    #: proposal_id, and a matching CONFIRM immediately writes a `stop_event`-shaped entry -- unless
    #: `safestop_responds` is False, simulating "og-safestop is not running" for the 503 test.
    safestop_responds: bool = True
    notifications: list[dict[str, Any]] = field(default_factory=list)
    _pending_stop_proposals: dict[str, tuple[str, str]] = field(default_factory=dict, init=False)
    stop_events: dict[tuple[str, str], tuple[str, datetime]] = field(default_factory=dict)
    #: The `FakeCallStore` the dispatch-call routes write to (`opengrid.calls`, D-33), linked by the
    #: fixtures so `list_active_as_deployments` shows what those calls deployed.
    call_store: Any = None
    #: MANUAL_TARGET trace rows `(trace_id, payload, created_at)` served by `manual_target_rows`.
    manual_target_rows_data: list[tuple[Any, dict[str, Any], datetime]] = field(default_factory=list)
    #: og.stop_event rows `(stop_event_id, scope_kind, scope_ref, action, created_at)` for `stop_event_rows`.
    stop_event_rows_data: list[tuple[Any, ...]] = field(default_factory=list)
    #: trace ids `trace_recorded` reports as NOT in og.trace (the backend journaled them).
    unrecorded_trace_ids: set[str] = field(default_factory=set)
    #: request_id -> trace_id for writes that committed although the append raised (lost ack).
    committed_request_ids: dict[str, UUID] = field(default_factory=dict)
    #: (scope_kind, scope_ref) -> og.owner_charge_window row (D-30).
    charge_windows: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    #: ("HUB", hub_id) / ("BANK", bank_id) -> {hub_id, bank_id, zone, feeder_id, substation_id}.
    topology: dict[tuple[str, str | None], dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.alerts = [
            Alert(
                id=1,
                rule="feed_stale",
                severity="warning",
                summary="ERCOT price feed stale",
                opened_at=_now(),
            )
        ]

    async def list_hubs(self, *, zone, bank_id, health, limit, offset) -> list[dict[str, Any]]:
        return [
            {
                "hub_id": SAMPLE_HUB_ID,
                "bank_id": SAMPLE_BANK_ID,
                "zone": "LZ_SOUTH",
                "soc_kwh": 10.0,
                "p_kw": -2.0,
                "health": "online",
                "lease_epoch": 0,
                "lease_expires_at": None,
                "last_command_id": None,
                "last_seen_at": _now(),
                "fault_code": None,
            }
        ]

    async def get_hub(self, hub_id: str) -> dict[str, Any] | None:
        if hub_id != SAMPLE_HUB_ID:
            return None
        return {
            "hub_id": hub_id,
            "bank_id": SAMPLE_BANK_ID,
            "zone": "LZ_SOUTH",
            "e_kwh": 13.5,
            "r_kwh": 2.7,
            "rated_p_kw": 5.0,
            "eta_c": 0.9487,
            "eta_d": 0.9487,
            "lat": None,
            "lon": None,
            "soc_kwh": 10.0,
            "p_kw": -2.0,
            "health": "online",
            "lease_epoch": 3,
            "lease_expires_at": None,
            "last_command_id": None,
            "last_seen_at": _now(),
            "fault_code": None,
        }

    async def get_bank(self, bank_id: str) -> BankAggregate | None:
        if bank_id != SAMPLE_BANK_ID:
            return None
        return BankAggregate(
            bank_id=bank_id,
            zone="LZ_SOUTH",
            kva_rating=75.0,
            reserve_kva=0.0,
            feeder_id="F-1",
            member_hub_count=50,
            load_kva_estimate=40.0,
        )

    async def hub_telemetry_sparkline(self, hub_id: str, *, minutes: int) -> list[dict[str, Any]]:
        return [{"ts": _now(), "soc_kwh": 10.0, "p_kw": -2.0}]

    async def feed_series(self, *, product, series, t0, t1) -> list[FeedObs]:
        return [
            FeedObs(
                source="ERCOT",
                product="np6-905-cd",
                series="HB_HUBAVG",
                ts=_now(),
                value=32.5,
                unit="usd_per_mwh",
                quality="GOOD",
                recorded_at=_now(),
            )
        ]

    async def feed_statuses(self) -> list[FeedStatusRow]:
        return [
            FeedStatusRow(
                source="ERCOT",
                product="np6-905-cd",
                last_value_at=_now(),
                last_success_at=_now(),
                consecutive_failures=0,
                breaker_open=False,
                active_key="PRIMARY",
            )
        ]

    async def forecast_series(self, *, series_key: str, kind: str) -> list[ForecastPoint]:
        return [
            ForecastPoint(
                series_key=series_key,
                kind=kind,
                interval_start_utc=_now(),
                horizon_step=0,
                p10=20.0,
                p50=30.0,
                p90=45.0,
                firm_fitness="FIRM_OK",
            )
        ]

    async def list_opportunities(self, *, state: str | None) -> list[Opportunity]:
        return [
            Opportunity(
                opportunity_id=uuid4(),
                contract_id=SAMPLE_CONTRACT_ID,
                window_start=_now(),
                window_end=_now(),
                requested_kw=Decimal("100"),
                state=state or "OFFERED",
                admitted_at=_now(),
            )
        ]

    async def list_obligations(self, *, state: str | None) -> list[Obligation]:
        return [
            Obligation(
                obligation_id=uuid4(),
                opportunity_id=uuid4(),
                contract_id=SAMPLE_CONTRACT_ID,
                service_type="ERCOT_ENERGY",
                tier="T2",
                window_start=_now(),
                window_end=_now(),
                committed_qty_kw=Decimal("100"),
                state=state or "COMMITTED",
            )
        ]

    async def count_active_commitments(self) -> int:
        return 1

    async def hub_health_summary(self) -> dict[str, int]:
        return {"total": 1, "online": 1, "stale": 0, "offline": 0}

    async def latest_plan(self) -> Plan | None:
        return Plan(
            plan_id=uuid4(),
            plan_mode="L-DA",
            gate_kind="SCHEDULED_15MIN",
            horizon_start=_now(),
            horizon_end=_now(),
            scenario_set=[{"p50": 30.0}],
            solver_status="OPTIMAL",
        )

    async def ledger_timeline(self, bank_id: str) -> list[Reservation]:
        return [
            Reservation(
                reservation_id=uuid4(),
                obligation_id=uuid4(),
                bank_id=SAMPLE_BANK_ID,
                kind="POWER_KW",
                amount=Decimal("50"),
                interval_start=_now(),
                interval_end=_now(),
                ledger_version=1,
            )
        ]

    async def list_grants(self, bank_id: str) -> list[Grant]:
        # `Grant.bank_id`/`Reservation.bank_id` are typed `UUID` (`opengrid.core.models.engine`,
        # matching `og.reservation`/`og.grant`'s `uuid` columns) while the fleet twin's `bank_id` is a
        # text code like "bank-01" (`og.bank.bank_id TEXT`, 02b S4.2) -- a pre-existing schema/model
        # mismatch outside `api`'s ownership (flagged in the final report). The fake stands in a real
        # UUID here rather than the text `bank_id` argument to avoid a spurious pydantic error in tests.
        return [
            Grant(
                grant_id=uuid4(),
                cycle_id="cycle-1",
                obligation_id=uuid4(),
                bank_id=SAMPLE_BANK_ID,
                granted_kw=Decimal("50"),
                is_headroom=False,
                ledger_version=1,
            )
        ]

    async def list_commitments(self, *, limit: int = 200) -> list[Commitment]:
        return [
            Commitment(
                commitment_id=uuid4(),
                obligation_id=uuid4(),
                plan_id=uuid4(),
                interval_start=_now(),
                interval_end=_now(),
                committed_kw=Decimal("50"),
                variable_kind="CONTINUOUS",
            )
        ]

    async def profitability_summary(self, *, service, day) -> list[dict[str, Any]]:
        return [
            {
                "obligation_id": uuid4(),
                "service_type": service or "ERCOT_ENERGY",
                "contract_id": SAMPLE_CONTRACT_ID,
                "interval_start": _now(),
                "interval_end": _now(),
                "revenue": Decimal("100"),
                "energy_cost": Decimal("10"),
                "degradation_cost": Decimal("1"),
                "penalty": Decimal("0"),
                "net_value": Decimal("89"),
                "rule_baseline_value": Decimal("80"),
                "forgone_upside": Decimal("0"),
            }
        ]

    async def invoice_lines(self, *, t0: date, t1: date) -> list[InvoiceLine]:
        return [
            InvoiceLine(
                invoice_line_id=uuid4(),
                contract_id=SAMPLE_CONTRACT_ID,
                obligation_id=uuid4(),
                period_start=t0,
                period_end=t1,
                line_type="ENERGY",
                amount=Decimal("42.00"),
            )
        ]

    async def performance_summary(self, *, t0: date, t1: date) -> list[Performance]:
        return [
            Performance(
                performance_id=uuid4(),
                obligation_id=uuid4(),
                interval_start=_now(),
                interval_end=_now(),
                compliance_pct=Decimal("0.98"),
                passed_threshold=True,
            )
        ]

    async def trace_events(self, *, event_class, t0, t1, limit) -> list[TraceRow]:
        return [
            TraceRow(
                trace_id=uuid4(),
                decision_type="OPERATOR_ACTION",
                event_class=event_class or "MANUAL_COMMAND",
                stream_id="operator_action:alice",
                seq=0,
                payload={"k": "v"},
                hash="deadbeef",
            )
        ]

    async def list_stream_ids(self) -> list[str]:
        return ["engine:cycle-1"]

    async def health_snapshot(self, *, heartbeat_miss_threshold_s: float) -> HealthSnapshot:
        return HealthSnapshot(
            processes=[ProcessHeartbeat(process="og-engine", pid=1, ts=_now(), status="ok")],
            feeds=await self.feed_statuses(),
            hub_health_counts={"online": 1},
            open_alerts=self.alerts,
        )

    async def list_alerts(self, *, open_only: bool) -> list[Alert]:
        return self.alerts

    async def ack_alert(self, alert_id: int, *, operator: str) -> Alert | None:
        for i, alert in enumerate(self.alerts):
            if alert.id == alert_id:
                acked = alert.model_copy(update={"acked_by": operator})
                self.alerts[i] = acked
                return acked
        return None

    async def current_ledger_version(self) -> int:
        return 1

    async def insert_command_batch(self, batch: CommandBatchRow) -> None:
        self.command_batches.append(batch)
        self.verdicts[batch.command_batch_id] = Verdict(
            verdict_id=uuid4(),
            command_batch_id=batch.command_batch_id,
            outcome=self.next_verdict_outcome,
            vetoed_rule_ids=list(self.next_vetoed_rule_ids),
            latency_ms=5,
            inputs_hash="deadbeef",
        )

    async def get_verdict(self, command_batch_id: UUID) -> Verdict | None:
        return self.verdicts.get(command_batch_id)

    async def notify(self, channel: str, payload: dict[str, Any]) -> None:
        self.notifications.append({"channel": channel, **payload})
        if payload["action"] == "PROPOSE":
            self._pending_stop_proposals[payload["proposal_id"]] = (payload["scope"], payload["scope_ref"])
        elif payload["action"] == "CONFIRM" and self.safestop_responds:
            scope_scope_ref = self._pending_stop_proposals.pop(payload["proposal_id"], None)
            if scope_scope_ref is not None:
                self.stop_events[scope_scope_ref] = ("ENGAGE", _now())

    async def latest_stop_event(self, scope_kind: str, scope_ref: str) -> tuple[str, datetime] | None:
        return self.stop_events.get((scope_kind, scope_ref))

    async def retention_policy(self) -> list[dict[str, Any]]:
        return [
            {"event_class": k, "retention_days": v, "prune_after_checkpoint": True}
            for k, v in self.retention.items()
        ]

    async def upsert_retention_policy(self, event_class: str, retention_days: int) -> None:
        self.retention[event_class] = retention_days

    async def insert_operator_action(
        self,
        *,
        operator_ref,
        action_kind,
        target_ref,
        tier,
        reason,
        trace_id,
        confirmed_at,
        approver_ref=None,
        created_at=None,
    ) -> UUID:
        action_id = uuid4()
        self.operator_actions.append(
            {
                "operator_action_id": action_id,
                # PgStore: coalesce(created_at, now()) -- now() is the INSERT time
                "created_at": created_at if created_at is not None else datetime.now(UTC),
                "operator_ref": operator_ref,
                "action_kind": action_kind,
                "target_ref": target_ref,
                "tier": tier,
                "confirmed_at": confirmed_at,
                "approver_ref": approver_ref if approver_ref is not None else operator_ref,
                "trace_id": trace_id,
            }
        )
        return action_id

    async def list_charge_windows(self) -> list[dict[str, Any]]:
        order = ["FLEET", "PROVIDER", "ZONE", "SUBSTATION", "FEEDER", "BANK", "HUB"]
        return [
            dict(row)
            for _key, row in sorted(
                self.charge_windows.items(), key=lambda kv: (order.index(kv[0][0]), kv[0][1])
            )
        ]

    async def set_charge_window(
        self, scope_kind: str, scope_ref: str, windows: list[str], *, updated_by: str
    ) -> None:
        self.charge_windows[(scope_kind, scope_ref)] = {
            "scope_kind": scope_kind,
            "scope_ref": scope_ref,
            "windows": list(windows),
            "updated_by": updated_by,
            "updated_at": datetime(2026, 9, 26, 18, 0, tzinfo=UTC),
        }

    async def delete_charge_window(self, scope_kind: str, scope_ref: str) -> bool:
        return self.charge_windows.pop((scope_kind, scope_ref), None) is not None

    async def charge_window_topology(
        self, *, hub_id: str | None, bank_id: str | None
    ) -> dict[str, Any] | None:
        key = ("HUB", hub_id) if hub_id is not None else ("BANK", bank_id)
        return self.topology.get(key)

    async def stop_event_rows(self) -> list[tuple[Any, ...]]:
        return list(self.stop_event_rows_data)

    async def hub_banks_zones(self, hub_ids: list[str]) -> dict[str, tuple[str, str]]:
        return {h: (SAMPLE_BANK_ID, "LZ_SOUTH") for h in hub_ids if h == SAMPLE_HUB_ID}

    async def trace_recorded(self, trace_id: UUID) -> bool:
        return str(trace_id) not in self.unrecorded_trace_ids

    async def manual_target_trace_id(self, request_id: str) -> UUID | None:
        return self.committed_request_ids.get(request_id)

    async def manual_target_rows(self) -> list[tuple[Any, dict[str, Any], datetime]]:
        """Test-set `manual_target_rows_data` (the SQL itself runs in the integration suite)."""
        return list(self.manual_target_rows_data)

    async def alert_ack_states(self, alert_ids: list[int]) -> dict[int, str | None]:
        return {a.id: a.acked_by for a in self.alerts if a.id is not None and a.id in alert_ids}

    def _matching_alerts(self, query: AlertQuery) -> list[Alert]:
        def ok(a: Alert) -> bool:
            return (
                (not query.open_only or a.cleared_at is None)
                and (query.severity is None or a.severity == query.severity)
                and (query.rule is None or a.rule == query.rule)
                and (query.scope_kind is None or a.scope_kind == query.scope_kind)
                and (query.scope_ref is None or a.scope_ref == query.scope_ref)
            )

        return sorted((a for a in self.alerts if ok(a)), key=lambda a: (a.opened_at, a.id or 0), reverse=True)

    async def query_alerts(self, query: AlertQuery, *, limit: int, offset: int) -> tuple[list[Alert], int]:
        matching = self._matching_alerts(query)
        return matching[offset : offset + limit], len(matching)

    async def alert_group_counts(self, query: AlertQuery) -> list[dict[str, Any]]:
        counts: dict[tuple[str, str | None, str | None], int] = {}
        for a in self._matching_alerts(query):
            key = (a.rule, a.scope_kind, a.scope_ref)
            counts[key] = counts.get(key, 0) + 1
        return [
            {"rule": r, "scope_kind": k, "scope_ref": s, "count": n}
            for (r, k, s), n in sorted(
                counts.items(), key=lambda kv: (-kv[1], kv[0][0], str(kv[0][1]), str(kv[0][2]))
            )
        ]

    async def list_active_as_deployments(self) -> list[dict[str, Any]]:
        """Deployments are written only by `opengrid.calls`; the API fixtures link this fake to the
        `FakeCallStore` those writes land in (`call_store`)."""
        if self.call_store is None:
            return []
        return [
            {
                "deployment_id": d.deployment_id,
                "obligation_id": d.obligation_id,
                "start_at": d.start_at,
                "end_at": d.end_at,
                "source": d.source,
                "requested_by": d.requested_by,
                "reason": d.reason,
                "requested_kw": d.requested_kw,
                "call_id": d.call_id,
            }
            for d in self.call_store.active_deployments()
        ]


class FakeTraceBackend:
    """In-memory `opengrid.trace.TraceBackend` -- exercises the real hash-chain logic in
    `TraceStore`/`opengrid.core.tracehash` without touching Postgres."""

    def __init__(self) -> None:
        self._rows: dict[str, list[ChainRecord]] = {}
        self._trace_ids: dict[UUID, str] = {}

    async def last_head(self, stream_id: str) -> tuple[int, str | None]:
        rows = self._rows.get(stream_id, [])
        if not rows:
            return -1, None
        return rows[-1].seq, rows[-1].hash

    async def insert_trace_row(
        self,
        *,
        trace_id: UUID,
        stream_id: str,
        seq: int,
        decision_type: str,
        event_class: str,
        payload: dict[str, Any],
        reason_codes: list[str] | None,
        prev_hash: str | None,
        record_hash: str,
        created_at: datetime,
    ) -> None:
        self._rows.setdefault(stream_id, []).append(
            ChainRecord(
                seq=seq,
                decision_type=decision_type,
                event_class=event_class,
                payload=payload,
                prev_hash=prev_hash,
                hash=record_hash,
            )
        )
        self._trace_ids[trace_id] = stream_id

    async def exists_preimage(self, decision_ref: UUID) -> bool:
        return any(
            row.payload.get("decision_ref") == str(decision_ref)
            for rows in self._rows.values()
            for row in rows
        )

    async def fetch_range(self, stream_id: str, *, from_seq: int) -> list[ChainRecord]:
        return [r for r in self._rows.get(stream_id, []) if r.seq >= from_seq]

    async def stream_ids(self) -> list[str]:
        return list(self._rows.keys())

    async def insert_checkpoint(self, **_kwargs: Any) -> None:
        return None

    async def prune_before(self, stream_id: str, *, keep_from_seq: int) -> int:
        before = len(self._rows.get(stream_id, []))
        self._rows[stream_id] = [r for r in self._rows.get(stream_id, []) if r.seq >= keep_from_seq]
        return before - len(self._rows[stream_id])

    async def retention_days_for(self, event_class: str) -> int:
        return 400
