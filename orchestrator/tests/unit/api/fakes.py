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
    BankAggregate,
    CustomerSummary,
    FeedStatusRow,
    ForecastPoint,
    HealthSnapshot,
    ProcessHeartbeat,
)
from opengrid.core.models.engine import (
    Contract,
    InvoiceLine,
    Obligation,
    Opportunity,
    Performance,
    Plan,
    Reservation,
    TraceRow,
)
from opengrid.core.models.platform import Alert, FeedObs, Hub, HubState
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
    contracts: dict[UUID, Contract] = field(default_factory=dict)
    operator_actions: list[dict[str, Any]] = field(default_factory=list)
    retention: dict[str, int] = field(default_factory=lambda: {"OPERATOR_ACTION": 1825})

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
        self.contracts = {
            SAMPLE_CONTRACT_ID: Contract(
                contract_id=SAMPLE_CONTRACT_ID,
                customer_id=SAMPLE_CUSTOMER_ID,
                service_type="ERCOT_ENERGY",
                tier="T2",
                profile_ref="ercot-energy-profile@1",
                start_at=_now(),
                status="ACTIVE",
            )
        }

    async def list_hubs(self, *, zone, bank_id, health, limit, offset) -> list[HubState]:
        return [HubState(hub_id=SAMPLE_HUB_ID, soc_kwh=10.0, p_kw=-2.0, health="online", last_seen_at=_now())]

    async def get_hub(self, hub_id: str) -> tuple[Hub, HubState] | None:
        if hub_id != SAMPLE_HUB_ID:
            return None
        hub = Hub(hub_id=hub_id, bank_id=SAMPLE_BANK_ID, zone="LZ_SOUTH", e_kwh=13.5, r_kwh=2.7, p_kw=5.0)
        state = HubState(hub_id=hub_id, soc_kwh=10.0, p_kw=-2.0, health="online", last_seen_at=_now())
        return hub, state

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
                bank_id=uuid4(),
                kind="POWER_KW",
                amount=Decimal("50"),
                interval_start=_now(),
                interval_end=_now(),
                ledger_version=1,
            )
        ]

    async def profitability_summary(self, *, service, day) -> list[dict[str, Any]]:
        return [
            {
                "service_type": service or "ERCOT_ENERGY",
                "day": day or date.today(),
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

    async def list_customers(self) -> list[CustomerSummary]:
        return [
            CustomerSummary(customer_id=SAMPLE_CUSTOMER_ID, contract_count=1, service_types=["ERCOT_ENERGY"])
        ]

    async def list_contracts(self, *, customer_id: UUID | None) -> list[Contract]:
        return list(self.contracts.values())

    async def get_contract(self, contract_id: UUID) -> Contract | None:
        return self.contracts.get(contract_id)

    async def create_contract(self, contract: Contract) -> Contract:
        self.contracts[contract.contract_id] = contract
        return contract

    async def update_contract_status(self, contract_id: UUID, status: str) -> Contract | None:
        existing = self.contracts.get(contract_id)
        if existing is None:
            return None
        updated = existing.model_copy(update={"status": status})
        self.contracts[contract_id] = updated
        return updated

    async def get_obligation(self, obligation_id: UUID) -> Obligation | None:
        return None

    async def retention_policy(self) -> list[dict[str, Any]]:
        return [
            {"event_class": k, "retention_days": v, "prune_after_checkpoint": True}
            for k, v in self.retention.items()
        ]

    async def upsert_retention_policy(self, event_class: str, retention_days: int) -> None:
        self.retention[event_class] = retention_days

    async def insert_operator_action(
        self, *, operator_ref, action_kind, target_ref, tier, reason, trace_id, confirmed_at
    ) -> UUID:
        action_id = uuid4()
        self.operator_actions.append(
            {
                "operator_action_id": action_id,
                "operator_ref": operator_ref,
                "action_kind": action_kind,
                "target_ref": target_ref,
                "trace_id": trace_id,
            }
        )
        return action_id


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
