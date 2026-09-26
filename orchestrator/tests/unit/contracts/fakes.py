"""In-memory fakes for `opengrid.contracts`'s siblings, used by every test in this package
(BUILD.md task brief: "Use fakes for sibling modules"). `FakeContractsRepo` implements the
`ContractsRepo` protocol this package defines; `FakeTraceBackend` implements `opengrid.trace.store
.TraceBackend`, which that module's own docstring says is exactly what tests should do -- so tests
exercise the real `opengrid.trace.TraceStore` hash-chain logic, not a stand-in for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from opengrid.contracts.errors import ConcurrentUpdateError
from opengrid.core.models.engine import (
    Contract,
    Obligation,
    ObligationState,
    Opportunity,
    ProductRule,
    RenominationPoint,
)
from opengrid.core.tracehash import ChainRecord


@dataclass
class FakeContractsRepo:
    contracts: dict[UUID, Contract] = field(default_factory=dict)
    product_rules: dict[UUID, list[ProductRule]] = field(default_factory=dict)
    opportunities: dict[UUID, Opportunity] = field(default_factory=dict)
    obligations: dict[UUID, Obligation] = field(default_factory=dict)
    renomination_points: dict[UUID, RenominationPoint] = field(default_factory=dict)

    # -- contract / product_rule --------------------------------------------------------------

    async def get_contract(self, contract_id: UUID) -> Contract | None:
        return self.contracts.get(contract_id)

    async def list_contracts(
        self, *, customer_id: UUID | None = None, service_type: str | None = None, status: str | None = None
    ) -> list[Contract]:
        rows = list(self.contracts.values())
        if customer_id is not None:
            rows = [r for r in rows if r.customer_id == customer_id]
        if service_type is not None:
            rows = [r for r in rows if r.service_type == service_type]
        if status is not None:
            rows = [r for r in rows if r.status == status]
        return rows

    async def list_customer_ids(self) -> list[UUID]:
        return sorted({c.customer_id for c in self.contracts.values()}, key=str)

    async def upsert_contract(self, contract: Contract) -> Contract:
        self.contracts[contract.contract_id] = contract
        return contract

    async def get_product_rules(self, contract_id: UUID) -> list[ProductRule]:
        return list(self.product_rules.get(contract_id, []))

    async def upsert_product_rule(self, rule: ProductRule) -> ProductRule:
        rules = self.product_rules.setdefault(rule.contract_id, [])
        rules[:] = [r for r in rules if r.product_code != rule.product_code]
        rules.append(rule)
        return rule

    # -- opportunity / obligation --------------------------------------------------------------

    async def create_opportunity_and_obligation(
        self, opportunity: Opportunity, obligation: Obligation
    ) -> None:
        self.opportunities[opportunity.opportunity_id] = opportunity
        self.obligations[obligation.obligation_id] = obligation

    async def get_opportunity(self, opportunity_id: UUID) -> Opportunity | None:
        return self.opportunities.get(opportunity_id)

    async def update_opportunity_state(
        self,
        opportunity_id: UUID,
        *,
        state: str,
        reason_code: str | None,
        decided_at: datetime,
        gate_id: UUID | None = None,
    ) -> Opportunity:
        current = self.opportunities[opportunity_id]
        updated = current.model_copy(
            update={
                "state": state,
                "reason_code": reason_code,
                "decided_at": decided_at,
                "gate_id": gate_id if gate_id is not None else current.gate_id,
            }
        )
        self.opportunities[opportunity_id] = updated
        return updated

    async def get_obligation(self, obligation_id: UUID) -> Obligation | None:
        return self.obligations.get(obligation_id)

    async def get_obligation_by_opportunity(self, opportunity_id: UUID) -> Obligation | None:
        for obligation in self.obligations.values():
            if obligation.opportunity_id == opportunity_id:
                return obligation
        return None

    async def update_obligation_state(
        self,
        obligation_id: UUID,
        *,
        to_state: ObligationState,
        reason_code: str | None,
        expected_version: int,
        at_risk: bool | None = None,
    ) -> Obligation:
        current = self.obligations[obligation_id]
        if current.version != expected_version:
            raise ConcurrentUpdateError(obligation_id, expected_version)
        updated = current.model_copy(
            update={
                "state": to_state,
                "last_reason_code": reason_code,
                "version": current.version + 1,
                "at_risk": current.at_risk if at_risk is None else at_risk,
            }
        )
        self.obligations[obligation_id] = updated
        return updated

    async def set_obligation_at_risk(self, obligation_id: UUID, at_risk: bool) -> Obligation:
        if obligation_id not in self.obligations:
            raise LookupError(f"no such obligation: {obligation_id}")
        updated = self.obligations[obligation_id].model_copy(update={"at_risk": at_risk})
        self.obligations[obligation_id] = updated
        return updated

    async def active_obligations_by_interval(
        self,
        interval_start: datetime,
        interval_end: datetime,
        *,
        service_type: str | None = None,
        states: tuple[ObligationState, ...] = ("COMMITTED", "DELIVERING"),
    ) -> list[Obligation]:
        rows = [
            o
            for o in self.obligations.values()
            if o.state in states and o.window_start < interval_end and o.window_end > interval_start
        ]
        if service_type is not None:
            rows = [o for o in rows if o.service_type == service_type]
        return sorted(rows, key=lambda o: o.window_start)

    async def unselected_offered_before(self, cutoff: datetime) -> list[Opportunity]:
        return [o for o in self.opportunities.values() if o.state == "OFFERED" and o.window_start <= cutoff]

    async def find_opportunity_by_window(
        self, contract_id: UUID, window_start: datetime, window_end: datetime
    ) -> Opportunity | None:
        for o in self.opportunities.values():
            if o.contract_id == contract_id and o.window_start == window_start and o.window_end == window_end:
                return o
        return None

    # -- renomination_point ---------------------------------------------------------------------

    async def due_renomination_points(self, as_of: datetime) -> list[RenominationPoint]:
        return [
            p for p in self.renomination_points.values() if p.scheduled_at <= as_of and p.exercised_at is None
        ]

    async def get_renomination_point(self, renomination_point_id: UUID) -> RenominationPoint | None:
        return self.renomination_points.get(renomination_point_id)

    async def mark_renomination_exercised(
        self,
        renomination_point_id: UUID,
        *,
        outcome: str,
        plan_id: UUID | None,
        exercised_at: datetime,
    ) -> RenominationPoint:
        current = self.renomination_points[renomination_point_id]
        updated = current.model_copy(
            update={"outcome": outcome, "plan_id": plan_id, "exercised_at": exercised_at}
        )
        self.renomination_points[renomination_point_id] = updated
        return updated


@dataclass
class FakeTraceBackend:
    """Minimal in-memory `opengrid.trace.store.TraceBackend` -- enough for `TraceStore.append` to
    build a real hash chain, so tests assert against actual chained trace rows."""

    rows: list[dict[str, Any]] = field(default_factory=list)

    async def last_head(self, stream_id: str) -> tuple[int, str | None]:
        stream_rows = [r for r in self.rows if r["stream_id"] == stream_id]
        if not stream_rows:
            return -1, None
        last = stream_rows[-1]
        return last["seq"], last["hash"]

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
        self.rows.append(
            {
                "trace_id": trace_id,
                "stream_id": stream_id,
                "seq": seq,
                "decision_type": decision_type,
                "event_class": event_class,
                "payload": payload,
                "reason_codes": reason_codes,
                "prev_hash": prev_hash,
                "hash": record_hash,
                "created_at": created_at,
            }
        )

    async def exists_preimage(self, decision_ref: UUID) -> bool:
        return any(r["trace_id"] == decision_ref for r in self.rows)

    async def fetch_range(self, stream_id: str, *, from_seq: int) -> list[ChainRecord]:
        return [
            ChainRecord(
                r["seq"], r["decision_type"], r["event_class"], r["payload"], r["prev_hash"], r["hash"]
            )
            for r in self.rows
            if r["stream_id"] == stream_id and r["seq"] >= from_seq
        ]

    async def stream_ids(self) -> list[str]:
        return sorted({r["stream_id"] for r in self.rows})

    async def insert_checkpoint(
        self,
        *,
        checkpoint_id: UUID,
        checkpoint_at: datetime,
        stream_heads: dict[str, dict[str, Any]],
        checkpoint_hash_hex: str,
    ) -> None:
        return None

    async def prune_before(self, stream_id: str, *, keep_from_seq: int) -> int:
        before = len(self.rows)
        self.rows = [r for r in self.rows if not (r["stream_id"] == stream_id and r["seq"] < keep_from_seq)]
        return before - len(self.rows)

    async def retention_days_for(self, event_class: str) -> int:
        return 400


def utc_now() -> datetime:
    return datetime.now(UTC)
