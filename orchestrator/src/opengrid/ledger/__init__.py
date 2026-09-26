"""opengrid.ledger -- single-writer reservation ledger (02a S4). Owner: allocator agent (BUILD.md S4).

Only this module inserts into `og.reservation` / updates its `released_at`. Enforces the one-buyer
invariant (K2, `opengrid.core.limits.check_one_buyer`) at `reserve()` time and the commitment-lock
invariant (K13, `opengrid.core.limits.check_commitment_lock`) at `release()`/`reduce()` time.

I/O (Postgres reads/writes) is isolated behind the `LedgerBackend` protocol so this module's decision
logic stays testable without a database, mirroring `opengrid.trace.store`'s split (BUILD.md S5a "pure
logic separated from I/O"). A real backend lives in `opengrid.ledger.pg_backend`; tests use an in-memory
fake. A process wires one `ReservationLedger` instance via `configure()`; the module-level `reserve` /
`release` / `ledger_version` functions are the fixed public interface other packages import
(`orchestrator/INTERFACES.md`).

Reservation key encoding: `reserve()`'s fixed signature carries only `obligation_id`,
`selected_kw_by_interval` and `plan_id` -- no separate bank/interval arguments -- so each dict key packs
`"<bank_id>|<interval_start_iso>|<interval_end_iso>"` (see `encode_interval_key`/`decode_interval_key`).
"""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID, uuid4

from opengrid.core.limits import LimitResult, check_commitment_lock, check_one_buyer

#: K13 (02a S4.3): reason codes that may ever reduce or release a committed reservation.
#: `R-AS-RELEASE` additionally requires `as_release_enabled=True` (default off, review S7.4).
ALLOWED_RELEASE_REASONS: frozenset[str] = frozenset(
    {
        "R-COMMIT-LOCK-OVERRIDE-L0",
        "R-COMMIT-LOCK-OVERRIDE-L1",
        "R-COMMIT-LOCK-OVERRIDE-L2",
        "R-COMMIT-LOCK-INFEASIBLE",
        "R-AS-RELEASE",
        "R-FULFILLED",
        "R-SETTLED",
        "R-SUBSTITUTION",
    }
)

#: Reasons that are a hold/substitution rather than a reduction; `check_commitment_lock`'s floor check
#: still applies to `R-SUBSTITUTION` for the *vacated* bank, but the *replacement* reservation is exempt
#: since the obligation's total is preserved (review S3: "substitution of hubs within the same
#: obligation allowed, switching to a different obligation forbidden").
_SUBSTITUTION_REASON = "R-SUBSTITUTION"


#: `og.commitment.reason_code` for the equality-freeze rows `reserve()` writes (02a S1.6 column default:
#: the selection gate produced the commitment).
_COMMITMENT_REASON = "R-GATE-SELECT"

#: Reason for releasing a reservation whose obligation never reached `COMMITTED` (no commitment row):
#: the commit did not complete, which is the `SELECTED -> REJECTED` edge's code (02a S2.1).
_UNCOMMITTED_RELEASE_REASON = "R-COMMIT-LOCK-INFEASIBLE"

#: `reserve()` reason code when the obligation already holds active reservations (K13: a committed
#: obligation is never re-reserved on top of itself; re-nomination supersedes, it does not stack).
R_ALREADY_COMMITTED = "R-COMMIT-ALREADY-COMMITTED"


class ReservationError(Exception):
    """Raised by `reserve()` with `.reason_code` set (e.g. `R-COMMIT-LOCK-INFEASIBLE`)."""

    def __init__(self, reason_code: str, detail: dict[str, object] | None = None) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.detail: dict[str, object] = detail or {}


class CommitmentLockViolation(ReservationError):  # noqa: N818 -- name fixed by BUILD.md's task brief (K13)
    """K13: raised by `release()`/`reduce()` when `reason_code` is not one of
    `ALLOWED_RELEASE_REASONS`, or is `R-AS-RELEASE` while the feature is disabled (default)."""


def encode_interval_key(bank_id: str, interval_start: datetime, interval_end: datetime) -> str:
    """Pack a (bank, interval) pair into the string key `reserve()`'s `selected_kw_by_interval` uses."""
    return f"{bank_id}|{interval_start.isoformat()}|{interval_end.isoformat()}"


def decode_interval_key(key: str) -> tuple[str, datetime, datetime]:
    """Inverse of `encode_interval_key`. Raises `ValueError` on a malformed key."""
    parts = key.split("|")
    if len(parts) != 3:
        raise ValueError(f"malformed reservation interval key: {key!r}")
    bank_id, start_s, end_s = parts
    return bank_id, datetime.fromisoformat(start_s), datetime.fromisoformat(end_s)


@dataclass(frozen=True, slots=True)
class ReservationRecord:
    """In-process/DB row shape mirroring `og.reservation` (migrations/0001_init.sql)."""

    reservation_id: UUID
    obligation_id: UUID
    bank_id: str
    interval_start: datetime
    interval_end: datetime
    amount_kw: Decimal
    ledger_version: int
    released_at: datetime | None = None
    release_reason: str | None = None

    @property
    def is_active(self) -> bool:
        return self.released_at is None


@dataclass(frozen=True, slots=True)
class CommitmentRecord:
    """Row shape of `og.commitment` (migrations/0001_init.sql): the equality freeze of one obligation's
    committed kW for one interval, summed across banks (02a S1.6/S2.2). Written only by `reserve()`, in
    the same transaction as the reservations it freezes."""

    commitment_id: UUID
    obligation_id: UUID
    plan_id: UUID
    interval_start: datetime
    interval_end: datetime
    committed_kw: Decimal
    variable_kind: str
    reason_code: str = _COMMITMENT_REASON


def build_commitments(
    obligation_id: UUID, plan_id: UUID, records: list[ReservationRecord], variable_kind: str
) -> list[CommitmentRecord]:
    """One `CommitmentRecord` per distinct interval in `records`, `committed_kw` = the sum of that
    interval's reservations across banks (kW)."""
    totals: dict[tuple[datetime, datetime], Decimal] = {}
    for record in records:
        key = (record.interval_start, record.interval_end)
        totals[key] = totals.get(key, Decimal(0)) + record.amount_kw
    return [
        CommitmentRecord(
            commitment_id=uuid4(),
            obligation_id=obligation_id,
            plan_id=plan_id,
            interval_start=start,
            interval_end=end,
            committed_kw=kw,
            variable_kind=variable_kind,
        )
        for (start, end), kw in sorted(totals.items())
    ]


class CapabilityProvider(Protocol):
    """The single source of `bank_id`/`interval`-scoped capability (`opengrid.fleet.capability`, 02b
    S4). Injected rather than imported directly so the ledger's decision logic is DB/fleet-free for
    unit and property tests."""

    async def capability_kw(self, bank_id: str, interval_start: datetime) -> Decimal: ...


@dataclass(frozen=True, slots=True)
class GrantRecord:
    """In-process/DB row shape mirroring `og.grant` (migrations/0001_init.sql, bank_id widened to text
    by migrations/0004_bank_id_text.sql -- merge task A1). Insert-only: the allocator's 2 s cycle
    (`opengrid.allocator.run_cycle`) proposes a fresh set of grants every cycle; nothing here ever
    updates or deletes a row (02a S7: "insert-only `og.grant` rows")."""

    grant_id: UUID
    cycle_id: str
    bank_id: str
    granted_kw: Decimal
    ledger_version: int
    obligation_id: UUID | None = None
    is_headroom: bool = False
    command_batch_id: UUID | None = None


class GrantBackend(Protocol):
    """Postgres persistence contract for `og.grant` (merge task A3: gives `opengrid.engine` a real
    `LedgerGateway.persist_grants` to wire into `opengrid.allocator`, instead of `run_cycle` raising
    `NotImplementedError` for lack of a grant-persistence gateway, `INTERFACES.md`)."""

    async def insert_grants(self, records: list[GrantRecord]) -> None: ...


class LedgerBackend(Protocol):
    """Postgres persistence contract for `og.reservation`. Isolates I/O per BUILD.md S5a; a real
    implementation (`opengrid.ledger.pg_backend.PgLedgerBackend`) uses `SELECT ... FOR UPDATE` inside a
    serializable transaction; tests use an in-memory fake (see `tests/unit/ledger/conftest.py`)."""

    def write_guard(self) -> AbstractAsyncContextManager[None]:
        """Cross-process mutual exclusion for one check-then-write sequence (K2 across `og-engine`
        processes; the in-process `asyncio.Lock` only covers one process)."""
        ...

    async def next_version(self) -> int:
        """Atomically allocate and return the next monotonic ledger version."""
        ...

    async def current_version(self) -> int:
        """The latest durably written ledger version (0 for an empty ledger)."""
        ...

    async def active_reservations(self, bank_id: str, interval_start: datetime) -> list[ReservationRecord]:
        """Active (`released_at IS NULL`) reservations for a bank/interval, row-locked for a writer."""
        ...

    async def reservations_for_obligation(self, obligation_id: UUID) -> list[ReservationRecord]: ...

    async def get_reservation(self, reservation_id: UUID) -> ReservationRecord | None: ...

    async def insert_reservations(
        self, records: list[ReservationRecord], commitments: list[CommitmentRecord]
    ) -> None:
        """Insert reservations and their commitment rows atomically (one transaction)."""
        ...

    async def mark_released(self, reservation_id: UUID, *, reason: str, version: int) -> None: ...

    async def release_uncommitted(self, *, reason: str, version: int) -> int:
        """Release every active reservation whose obligation has no active commitment row; returns
        the count released."""
        ...


@dataclass
class _ReadCache:
    """In-memory read cache of active reservations, keyed by (bank_id, interval_start), for the 2 s
    allocator's `free_headroom` reads (BUILD.md: "in-memory read cache for the 2 s allocator"). Since
    this module is the sole writer, the cache is kept authoritative by updating it on every write --
    it is never the source of truth for a write decision, only for `free_headroom` reads."""

    by_bank_interval: dict[tuple[str, datetime], list[ReservationRecord]] = field(default_factory=dict)
    by_obligation: dict[UUID, list[ReservationRecord]] = field(default_factory=dict)
    by_id: dict[UUID, ReservationRecord] = field(default_factory=dict)

    def put(self, record: ReservationRecord) -> None:
        self.by_id[record.reservation_id] = record
        key = (record.bank_id, record.interval_start)
        bucket = [r for r in self.by_bank_interval.get(key, []) if r.reservation_id != record.reservation_id]
        bucket.append(record)
        self.by_bank_interval[key] = bucket
        ob_bucket = [
            r
            for r in self.by_obligation.get(record.obligation_id, [])
            if r.reservation_id != record.reservation_id
        ]
        ob_bucket.append(record)
        self.by_obligation[record.obligation_id] = ob_bucket

    def active(self, bank_id: str, interval_start: datetime) -> list[ReservationRecord]:
        return [r for r in self.by_bank_interval.get((bank_id, interval_start), []) if r.is_active]

    def for_obligation(self, obligation_id: UUID) -> list[ReservationRecord]:
        return list(self.by_obligation.get(obligation_id, []))

    def get(self, reservation_id: UUID) -> ReservationRecord | None:
        return self.by_id.get(reservation_id)


class ReservationLedger:
    """Single-writer facade over `LedgerBackend`. One instance per `og-engine` process (02a S4:
    "one Python module, one process, not a DB permission"). Concurrent callers within the process
    serialize on `self._write_lock`; concurrent *processes* rely on the backend's row locks /
    serializable transaction (`PgLedgerBackend`) -- see BUILD.md's "Handles many concurrent
    obligations from many customers."
    """

    def __init__(
        self,
        backend: LedgerBackend,
        capability: CapabilityProvider,
        *,
        as_release_enabled: bool = False,
        grant_backend: GrantBackend | None = None,
    ):
        self._backend = backend
        self._capability = capability
        self._as_release_enabled = as_release_enabled
        self._grant_backend = grant_backend
        self._cache = _ReadCache()
        self._write_lock = asyncio.Lock()
        self._version = 0
        self._version_loaded = False

    async def _bump_version(self) -> int:
        self._version = await self._backend.next_version()
        self._version_loaded = True
        return self._version

    async def ledger_version(self) -> int:
        """The durable ledger version. Loaded from the backend on first use, so a restarted process
        stamps the same version the guardian reads independently (G-09) instead of restarting at 0."""
        if not self._version_loaded:
            self._version = max(self._version, await self._backend.current_version())
            self._version_loaded = True
        return self._version

    async def reservations_for_obligation(self, obligation_id: UUID) -> list[ReservationRecord]:
        """All reservations (active and released) an obligation has ever held, cache-first."""
        cached = self._cache.for_obligation(obligation_id)
        if cached:
            return cached
        return await self._backend.reservations_for_obligation(obligation_id)

    async def free_headroom(self, bank_id: str, interval_start: datetime) -> Decimal:
        """K2 read path: `capability(bank, t) - sum(active reservations on bank at t)`. Read-only;
        served from the in-memory cache warmed by prior writes, per 02a S4.1."""
        capability_kw = await self._capability.capability_kw(bank_id, interval_start)
        reserved = sum((r.amount_kw for r in self._cache.active(bank_id, interval_start)), Decimal(0))
        return capability_kw - reserved

    async def reserve(
        self,
        obligation_id: UUID,
        selected_kw_by_interval: dict[str, Decimal],
        plan_id: UUID,
        *,
        variable_kind: str = "CONTINUOUS",
    ) -> None:
        """The one-buyer check (K2) + commitment-lock entry point (02a S4.1/4.2). Writes the obligation's
        reservations and its equality-freeze `og.commitment` rows (one per interval, kW summed across
        banks) in one transaction. Raises `ReservationError` with no partial writes on failure
        (all-or-nothing across the obligation's intervals); `R_ALREADY_COMMITTED` if the obligation
        already holds active reservations (K13)."""
        async with self._write_lock, self._backend.write_guard():
            parsed: list[tuple[str, datetime, datetime, Decimal]] = []
            for key, kw in selected_kw_by_interval.items():
                bank_id, interval_start, interval_end = decode_interval_key(key)
                parsed.append((bank_id, interval_start, interval_end, kw))

            held = await self._backend.reservations_for_obligation(obligation_id)
            if any(r.is_active for r in held):
                raise ReservationError(R_ALREADY_COMMITTED)

            for bank_id, interval_start, _interval_end, kw in parsed:
                existing = await self._backend.active_reservations(bank_id, interval_start)
                existing_kw = [float(r.amount_kw) for r in existing if r.obligation_id != obligation_id]
                capability_kw = await self._capability.capability_kw(bank_id, interval_start)
                result: LimitResult = check_one_buyer([*existing_kw, float(kw)], float(capability_kw))
                if not result.ok:
                    raise ReservationError(
                        "R-COMMIT-LOCK-INFEASIBLE",
                        {
                            "bank_id": bank_id,
                            "interval_start": interval_start.isoformat(),
                            "requested_kw": float(kw),
                            "reserved_by_others_kw": sum(existing_kw),
                            "capability_kw": float(capability_kw),
                        },
                    )

            version = await self._bump_version()
            records = [
                ReservationRecord(
                    reservation_id=uuid4(),
                    obligation_id=obligation_id,
                    bank_id=bank_id,
                    interval_start=interval_start,
                    interval_end=interval_end,
                    amount_kw=kw,
                    ledger_version=version,
                )
                for bank_id, interval_start, interval_end, kw in parsed
            ]
            commitments = build_commitments(obligation_id, plan_id, records, variable_kind)
            await self._backend.insert_reservations(records, commitments)
            for record in records:
                self._cache.put(record)

    async def release_uncommitted(self) -> int:
        """Release every active reservation whose obligation never got a commitment row (a commit that
        did not complete, or rows written before `reserve()` froze commitments). Such rows hold K2
        headroom for an obligation nothing will ever deliver. Safe under K13: an obligation without a
        commitment is not committed. Returns the number of reservations released. Intended for process
        start-up (`og-engine`), so the read cache is simply reset rather than patched."""
        async with self._write_lock:
            version = await self._bump_version()
            released = await self._backend.release_uncommitted(
                reason=_UNCOMMITTED_RELEASE_REASON, version=version
            )
            # With nothing released the bumped version was never written; resync to the durable one.
            self._version = await self._backend.current_version()
            self._cache = _ReadCache()
            return released

    async def release(self, reservation_id: UUID, reason_code: str) -> None:
        """Release a committed reservation in place (02a S1.9). K13: only `ALLOWED_RELEASE_REASONS`
        may ever reduce a committed reservation to zero; `R-AS-RELEASE` additionally requires
        `as_release_enabled` (review S7.4, default off -- TS-05-12)."""
        await self._reduce_or_release(reservation_id, new_amount_kw=Decimal(0), reason_code=reason_code)

    async def reduce(self, reservation_id: UUID, new_amount_kw: Decimal, reason_code: str) -> None:
        """Partial reduction of a committed reservation (e.g. an L0/L1/L2 partial cut before a
        substitute is found). Same K13 gate as `release()`, evaluated via
        `opengrid.core.limits.check_commitment_lock`."""
        await self._reduce_or_release(reservation_id, new_amount_kw=new_amount_kw, reason_code=reason_code)

    async def _reduce_or_release(
        self, reservation_id: UUID, *, new_amount_kw: Decimal, reason_code: str
    ) -> None:
        async with self._write_lock:
            record = self._cache.get(reservation_id) or await self._backend.get_reservation(reservation_id)
            if record is None or not record.is_active:
                raise ReservationError("R-RESERVATION-NOT-FOUND")

            lock_result = check_commitment_lock(
                new_kw=float(new_amount_kw),
                frozen_kw=float(record.amount_kw),
                prior_kw=float(record.amount_kw),
                reason_code=reason_code,
                allowed_release_reasons=ALLOWED_RELEASE_REASONS - {"R-AS-RELEASE"},
                as_release_enabled=self._as_release_enabled,
            )
            if not lock_result.ok:
                raise CommitmentLockViolation(reason_code)

            version = await self._bump_version()
            await self._backend.mark_released(reservation_id, reason=reason_code, version=version)
            self._cache.put(
                replace(record, released_at=_now(), release_reason=reason_code, ledger_version=version)
            )

    async def persist_grants(self, cycle_id: str, grants: list[GrantRecord]) -> None:
        """S7: write one cycle's proposed grants as insert-only `og.grant` rows (merge task A3). Each
        record is stamped with the ledger version current at call time, per grant, matching the
        reservation write path's convention (02a S1.9) -- callers that need one shared version across
        the whole batch should read `ledger_version()` once and pass matching `GrantRecord`s.

        Raises `RuntimeError` if no `grant_backend` was wired at construction (an engine-only capability
        -- unit tests exercising just the reservation path never need it)."""
        if self._grant_backend is None:
            raise RuntimeError("ReservationLedger was constructed without a grant_backend")
        if not grants:
            return
        async with self._write_lock:
            await self._grant_backend.insert_grants(grants)

    async def substitute(
        self,
        obligation_id: UUID,
        from_reservation_id: UUID,
        to_bank_id: str,
    ) -> UUID:
        """Move a committed obligation's reservation to a different bank/hub grouping for the same
        interval and amount (review S3: substitution within one obligation is allowed; switching to a
        different obligation is forbidden -- 02a S5.1 S5, ES05-S04). Releases the old reservation with
        the exempt `R-SUBSTITUTION` reason and inserts a replacement that preserves the obligation's
        committed total. Returns the new reservation id."""
        async with self._write_lock, self._backend.write_guard():
            record = self._cache.get(from_reservation_id) or await self._backend.get_reservation(
                from_reservation_id
            )
            if record is None or not record.is_active:
                raise ReservationError("R-RESERVATION-NOT-FOUND")
            if record.obligation_id != obligation_id:
                raise ReservationError("R-SUBSTITUTION-OBLIGATION-MISMATCH")

            existing = await self._backend.active_reservations(to_bank_id, record.interval_start)
            existing_kw = [float(r.amount_kw) for r in existing if r.obligation_id != obligation_id]
            capability_kw = await self._capability.capability_kw(to_bank_id, record.interval_start)
            result = check_one_buyer([*existing_kw, float(record.amount_kw)], float(capability_kw))
            if not result.ok:
                raise ReservationError("R-COMMIT-LOCK-INFEASIBLE")

            release_version = await self._bump_version()
            await self._backend.mark_released(
                from_reservation_id, reason=_SUBSTITUTION_REASON, version=release_version
            )
            self._cache.put(
                replace(
                    record,
                    released_at=_now(),
                    release_reason=_SUBSTITUTION_REASON,
                    ledger_version=release_version,
                )
            )

            insert_version = await self._bump_version()
            new_record = ReservationRecord(
                reservation_id=uuid4(),
                obligation_id=obligation_id,
                bank_id=to_bank_id,
                interval_start=record.interval_start,
                interval_end=record.interval_end,
                amount_kw=record.amount_kw,
                ledger_version=insert_version,
            )
            # Substitution moves where the committed kW is realized; the commitment row is unchanged
            # (02a S2.1: "never a `commitment` write").
            await self._backend.insert_reservations([new_record], [])
            self._cache.put(new_record)
            return new_record.reservation_id


def _now() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


# --------------------------------------------------------------------------------------------------
# Module-level facade: the fixed public interface (`orchestrator/INTERFACES.md`). A process wires a
# `ReservationLedger` once via `configure()`; every other package imports these functions directly.
# --------------------------------------------------------------------------------------------------

_instance: ReservationLedger | None = None


def configure(ledger: ReservationLedger) -> None:
    """Wire the process-wide singleton (called once from `opengrid.engine.main`)."""
    global _instance
    _instance = ledger


def _require_instance() -> ReservationLedger:
    if _instance is None:
        raise RuntimeError("opengrid.ledger.configure() must be called before use")
    return _instance


async def reserve(
    obligation_id: UUID,
    selected_kw_by_interval: dict[str, Decimal],
    plan_id: UUID,
    *,
    variable_kind: str = "CONTINUOUS",
) -> None:
    """Attempt to reserve `selected_kw_by_interval` for `obligation_id` against each interval's bank
    capability (K2: one-buyer check via `opengrid.core.limits.check_one_buyer`). On success, inserts the
    reservations and the equality-freeze `commitment` row(s) atomically (02a S2.1); the caller
    (`opengrid.selector.gate`) then transitions the obligation `SELECTED -> COMMITTED` through
    `opengrid.contracts`. On failure raises `ReservationError("R-COMMIT-LOCK-INFEASIBLE")` with nothing
    written, and the caller transitions `SELECTED -> REJECTED`.
    """
    await _require_instance().reserve(
        obligation_id, selected_kw_by_interval, plan_id, variable_kind=variable_kind
    )


async def release_uncommitted() -> int:
    """Release active reservations of obligations that hold no commitment row (see
    `ReservationLedger.release_uncommitted`). Called once at `og-engine` start-up."""
    return await _require_instance().release_uncommitted()


async def release(reservation_id: UUID, reason_code: str) -> None:
    """Release a reservation in place (02a S1.9: "released in place, historized via trace"). Only
    valid reason codes are `R-COMMIT-LOCK-OVERRIDE-L0/L1/L2`, `R-COMMIT-LOCK-INFEASIBLE`,
    `R-SUBSTITUTION`, or the disabled `R-AS-RELEASE` (K13)."""
    await _require_instance().release(reservation_id, reason_code)


async def ledger_version() -> int:
    """The current monotonic ledger version this allocator instance is at (02a S1.9)."""
    return await _require_instance().ledger_version()


async def free_headroom(bank_id: str, interval_start: datetime) -> Decimal:
    """`capability(bank, t) - committed(bank, t)` (02a S4.1/S5.1 S2). Read-only; used by the
    allocator's S2 to size the spot/headroom schedule."""
    return await _require_instance().free_headroom(bank_id, interval_start)


async def persist_grants(cycle_id: str, grants: list[GrantRecord]) -> None:
    """S7: insert-only write of one allocator cycle's proposed grants into `og.grant` (merge task A3).
    The engine-owned `LedgerGateway` adapter (`opengrid.allocator.gateways.LedgerGateway`) calls this
    once per cycle after `opengrid.allocator.run_cycle` produces its `CycleResult`."""
    await _require_instance().persist_grants(cycle_id, grants)
