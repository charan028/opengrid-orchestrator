"""`CallStore`: the I/O the dispatch-call core needs (implemented by `opengrid.calls.pg_store.PgCallStore`
over `og.dispatch_call` / `og.as_deployment`, and by in-memory fakes in tests)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from opengrid.calls.models import AwardView, CallRecord, MeasuredDelivery
from opengrid.health.model import AlertSeverity


class OverlapError(Exception):
    """`insert_accepted` found another uncancelled call overlapping the window (checked under a per-
    obligation lock, so two concurrent calls can never both deploy)."""


class IdempotencyKeyTakenError(Exception):
    """`insert_*` hit the (principal, idempotency_key) unique index: a concurrent replay won the race."""


class CallStore(Protocol):
    async def get_award(self, obligation_id: UUID) -> AwardView | None:
        """The called obligation with its contract's variant/utility and its own product duration."""
        ...

    async def find_toll_obligation(self, utility_id: str, at: datetime) -> UUID | None:
        """The utility's deployable tolling obligation whose window covers `at` (earliest first)."""
        ...

    async def toll_obligations(self, utility_id: str, since: datetime, until: datetime) -> list[AwardView]:
        """The utility's tolling obligations whose window starts in `[since, until)`, by window start."""
        ...

    async def find_by_key(self, principal: str, idempotency_key: str) -> tuple[CallRecord, str] | None:
        """The call a principal already made with this key, and the request fingerprint it stored."""
        ...

    async def count_calls_since(self, principal: str, since: datetime) -> int:
        """Calls (accepted or refused) this principal made since `since` -- the rate-limit count."""
        ...

    async def insert_accepted(self, record: CallRecord, *, fingerprint: str, source: str) -> CallRecord:
        """Atomically: lock the obligation, re-check overlap (raises `OverlapError`), insert the
        `og.as_deployment` row, the ACCEPTED ledger row and its `og.operator_action` audit row
        (MANUAL_COMMAND, TIER1, `operator_ref` = the principal). Returns the record as stored."""
        ...

    async def insert_refused(self, record: CallRecord, *, fingerprint: str) -> CallRecord:
        """Insert a REFUSED ledger row (no deployment)."""
        ...

    async def get_call(self, call_id: UUID) -> CallRecord | None: ...

    async def get_call_by_deployment(self, deployment_id: UUID) -> CallRecord | None: ...

    async def list_calls(
        self,
        *,
        utility_id: str | None = None,
        principal: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[CallRecord]:
        """Newest first."""
        ...

    async def end_deployment(self, deployment_id: UUID, *, end_at: datetime, now: datetime) -> bool:
        """End (`end_at` <= now, or not after its start: set `cancelled_at`) or shorten (a later `end_at`)
        an uncancelled deployment that has not ended yet. False when there is none."""
        ...

    async def delivery(self, deployment_id: UUID) -> MeasuredDelivery | None:
        """The call's MEASURED delivery (`opengrid.delivery`, D-38), None until it has been evaluated."""
        ...

    async def raise_alert(
        self, rule: str, severity: AlertSeverity, summary: str, detail: dict[str, Any]
    ) -> None:
        """Open an `og.alert`; an identical rule+scope alert still open and unacknowledged is not
        repeated (a refusal storm raises one alert)."""
        ...
