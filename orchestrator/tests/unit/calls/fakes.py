"""In-memory `opengrid.calls.ports.CallStore` for unit tests (no Postgres): awards, the call ledger, the
deployments the engine would read, operator actions, alerts and per-obligation delivery."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from opengrid.calls.models import AwardView, CallKind, CallRecord, Granted, MeasuredDelivery
from opengrid.calls.ports import IdempotencyKeyTakenError, OverlapError


def make_award(
    *,
    service_type: str = "REGULATED_CAPACITY",
    variant: str | None = "TOLLING",
    state: str = "COMMITTED",
    duration_minutes: int | None = 90,
    committed_kw: float | None = 26_000.0,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    utility_id: str | None = "AUSTIN_ENERGY",
    obligation_id: UUID | None = None,
) -> AwardView:
    return AwardView(
        obligation_id=obligation_id or uuid4(),
        service_type=service_type,
        variant=variant,
        state=state,
        duration_minutes=duration_minutes,
        committed_kw=committed_kw,
        window_start=window_start,
        window_end=window_end,
        utility_id=utility_id,
    )


@dataclass
class FakeDeployment:
    deployment_id: UUID
    obligation_id: UUID | None
    start_at: datetime
    end_at: datetime
    source: str
    requested_by: str
    reason: str
    requested_kw: float | None
    call_id: UUID | None
    cancelled_at: datetime | None = None


@dataclass
class FakeCallStore:
    awards: dict[UUID, AwardView] = field(default_factory=dict)
    calls: dict[UUID, tuple[CallRecord, str]] = field(default_factory=dict)
    deployments: dict[UUID, FakeDeployment] = field(default_factory=dict)
    operator_actions: list[dict[str, Any]] = field(default_factory=list)
    alerts: list[dict[str, Any]] = field(default_factory=list)
    #: Measured delivery per deployment id (what opengrid.delivery would have recorded).
    measured: dict[UUID, MeasuredDelivery] = field(default_factory=dict)
    #: Deprecated granted (planned) discharge per obligation (the `granted_*` status fields).
    delivery: dict[UUID, Granted] = field(default_factory=dict)
    fail_alerts: bool = False

    def add(self, award: AwardView) -> UUID:
        self.awards[award.obligation_id] = award
        return award.obligation_id

    def _with_deployment(self, record: CallRecord) -> CallRecord:
        d = self.deployments.get(record.deployment_id) if record.deployment_id else None
        if d is None:
            return record
        return record.model_copy(update={"end_at": d.end_at, "cancelled_at": d.cancelled_at})

    async def get_award(self, obligation_id: UUID) -> AwardView | None:
        return self.awards.get(obligation_id)

    async def toll_obligations(self, utility_id: str, since: datetime, until: datetime) -> list[AwardView]:
        return sorted(
            (
                a
                for a in self.awards.values()
                if a.utility_id == utility_id
                and a.variant == "TOLLING"
                and a.window_start is not None
                and since <= a.window_start < until
            ),
            key=lambda a: a.window_start or since,
        )

    async def find_toll_obligation(self, utility_id: str, at: datetime) -> UUID | None:
        for a in await self.toll_obligations(utility_id, at - timedelta(days=2), at + timedelta(days=1)):
            if (
                a.state in ("COMMITTED", "DELIVERING", "SHORTFALL")
                and a.window_start is not None
                and a.window_end is not None
                and a.window_start <= at < a.window_end
            ):
                return a.obligation_id
        return None

    async def find_by_key(self, principal: str, idempotency_key: str) -> tuple[CallRecord, str] | None:
        for record, fingerprint in self.calls.values():
            if record.principal == principal and record.idempotency_key == idempotency_key:
                return self._with_deployment(record), fingerprint
        return None

    async def count_calls_since(self, principal: str, since: datetime) -> int:
        return sum(
            1 for r, _ in self.calls.values() if r.principal == principal and (r.created_at or since) >= since
        )

    def _key_taken(self, record: CallRecord) -> bool:
        return record.idempotency_key is not None and any(
            r.principal == record.principal and r.idempotency_key == record.idempotency_key
            for r, _ in self.calls.values()
        )

    async def insert_accepted(self, record: CallRecord, *, fingerprint: str, source: str) -> CallRecord:
        if self._key_taken(record):
            raise IdempotencyKeyTakenError(record.principal)
        for d in self.deployments.values():
            same = d.obligation_id == record.obligation_id or (
                d.obligation_id is None and record.kind is CallKind.AS
            )
            if same and d.cancelled_at is None and d.start_at < record.end_at and d.end_at > record.start_at:
                raise OverlapError(str(record.obligation_id))
        deployment_id = uuid4()
        stored = record.model_copy(update={"deployment_id": deployment_id, "created_at": datetime.now(UTC)})
        self.deployments[deployment_id] = FakeDeployment(
            deployment_id=deployment_id,
            obligation_id=record.obligation_id,
            start_at=record.start_at,
            end_at=record.end_at,
            source=source,
            requested_by=record.principal,
            reason=record.reason,
            requested_kw=record.requested_kw,
            call_id=record.call_id,
        )
        self.calls[stored.call_id] = (stored, fingerprint)
        kind = "UTILITY_CALL" if record.kind is CallKind.UTILITY_CALL else "AS_DEPLOYMENT"
        self.operator_actions.append(
            {
                "operator_ref": record.principal,
                "target_ref": f"{kind}:{record.obligation_id}",
                "reason": record.reason,
                "trace_id": record.trace_id,
            }
        )
        return stored

    async def insert_refused(self, record: CallRecord, *, fingerprint: str) -> CallRecord:
        if self._key_taken(record):
            raise IdempotencyKeyTakenError(record.principal)
        stored = record.model_copy(update={"created_at": datetime.now(UTC)})
        self.calls[stored.call_id] = (stored, fingerprint)
        return stored

    async def get_call(self, call_id: UUID) -> CallRecord | None:
        found = self.calls.get(call_id)
        return self._with_deployment(found[0]) if found else None

    async def get_call_by_deployment(self, deployment_id: UUID) -> CallRecord | None:
        for record, _ in self.calls.values():
            if record.deployment_id == deployment_id:
                return self._with_deployment(record)
        return None

    async def list_calls(
        self,
        *,
        utility_id: str | None = None,
        principal: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[CallRecord]:
        rows = [
            self._with_deployment(r)
            for r, _ in self.calls.values()
            if (utility_id is None or r.utility_id == utility_id)
            and (principal is None or r.principal == principal)
            and (since is None or (r.created_at or since) >= since)
        ]
        rows.sort(key=lambda r: r.created_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        return rows[:limit]

    async def end_deployment(self, deployment_id: UUID, *, end_at: datetime, now: datetime) -> bool:
        d = self.deployments.get(deployment_id)
        if d is None or d.cancelled_at is not None or d.end_at <= now:
            return False
        if end_at <= now or end_at <= d.start_at:
            d.cancelled_at = now
        else:
            d.end_at = end_at
        return True

    async def measured_delivery(self, deployment_id: UUID) -> MeasuredDelivery | None:
        return self.measured.get(deployment_id)

    async def granted(self, obligation_id: UUID, start: datetime, end: datetime) -> Granted:
        return self.delivery.get(obligation_id, Granted(last_kw=None, kwh=0.0))

    async def raise_alert(self, rule: str, severity: str, summary: str, detail: dict[str, Any]) -> None:
        if self.fail_alerts:
            raise RuntimeError("alert store down")
        self.alerts.append({"rule": rule, "severity": severity, "summary": summary, "detail": detail})

    def active_deployments(self) -> list[FakeDeployment]:
        now = datetime.now(UTC)
        return [d for d in self.deployments.values() if d.cancelled_at is None and d.end_at > now]
