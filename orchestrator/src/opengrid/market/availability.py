"""Bank availability and K13 grandfathering (decision log D-37, migration 0046).

D-37: LZ_LCRA and LZ_RAYBN are regulated (NOIE) territory with NO real utility contract, so their banks are
`og.bank.availability = 'UNAVAILABLE'`, reason `REGULATED_NO_CONTRACT`: energy can't be sold into ERCOT
(K15), and there is no utility capacity contract to reserve them. That one column is the representation
the selector, the allocator, the guardian, the UI and the APIs all read (a hub inherits its bank's).

An UNAVAILABLE bank is offered and planned nothing: no candidate, no FREE headroom, no charging (idle hold).
It stays monitored (telemetry, alerts, health, safe stop, firmware, invariants).

K13 grandfathering: an obligation that was already COMMITTED/DELIVERING when its bank became unavailable,
and still holds a live reservation on it, completes there untouched. The rule, in one place:

    grandfathered(obligation, bank)  <=>  bank UNAVAILABLE
                                          AND obligation.created_at < bank.availability_since
                                          AND obligation.state IN (COMMITTED, DELIVERING, SHORTFALL)
                                          AND the obligation holds a live (unreleased) reservation on the bank
                                          AND its window has not ended.

`GRANDFATHERED_SQL` is that rule for the readers with a database (selector, engine/allocator, guardian:
each runs it on its own pool); `is_grandfathered` is the pure form for the first two conjuncts. A
grandfathered (obligation, bank) pair is exempt from the availability exclusion AND from the K15 territory
check (it was committed while the zone was ERCOT competitive). Nothing new can ever be grandfathered: the
predicate needs a reservation, and no new reservation is made on an unavailable bank.

Pure except for the SQL text (no I/O here).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from opengrid.core.models.engine import Contract
from opengrid.core.models.market import (
    AVAILABILITY_BADGE,
    AVAILABILITY_TEXT,
    AVAILABLE,
    REGULATED_NO_CONTRACT,
    SAMPLE_INACTIVE_LABEL,
    UNAVAILABLE,
    Availability,
    AvailabilityReason,
)
from opengrid.core.reasons import R_BANK_UNAVAILABLE


@dataclass(frozen=True, slots=True)
class BankAvailability:
    """One bank's `og.bank.availability` / `availability_reason` / `availability_since`."""

    bank_id: str
    availability: Availability = AVAILABLE
    reason: AvailabilityReason | None = None
    since: datetime | None = None

    @property
    def available(self) -> bool:
        return self.availability == AVAILABLE

    @property
    def badge(self) -> str | None:
        """The owner's short badge ("Regulated market - no contract"), or None when available."""
        return None if self.available else availability_badge(self.reason)

    @property
    def text(self) -> str | None:
        """The owner's detail/tooltip text, or None when available."""
        return None if self.available else availability_text(self.reason)


def availability_badge(reason: str | None) -> str:
    """The badge for an UNAVAILABLE reason (an unknown reason still reads as unavailable)."""
    return AVAILABILITY_BADGE.get(reason or "", "Unavailable")


def availability_text(reason: str | None) -> str:
    return AVAILABILITY_TEXT.get(reason or "", "Unavailable.")


def parse_availability(
    bank_id: str, availability: str | None, reason: str | None, since: datetime | None
) -> BankAvailability:
    """A DB row as `BankAvailability`. NULL (a database before 0046) is AVAILABLE, the column default. An
    unknown value is UNAVAILABLE (fail closed: never plan on a bank whose state cannot be read)."""
    if availability is None or availability == AVAILABLE:
        return BankAvailability(bank_id)
    known_reason: AvailabilityReason | None = (
        REGULATED_NO_CONTRACT if reason == REGULATED_NO_CONTRACT else None
    )
    return BankAvailability(bank_id, UNAVAILABLE, known_reason, since)


def availability_fields(availability: str | None, reason: str | None) -> dict[str, str | None]:
    """The operator/customer API fields (D-37), one shape everywhere: `availability`,
    `availability_reason`, `availability_text` (the owner's tooltip text) and `availability_badge` (the
    short badge). A row without the columns (a database before 0046, a test fake) reads AVAILABLE."""
    bank = parse_availability("", availability, reason, None)
    return {
        "availability": bank.availability,
        "availability_reason": bank.reason if not bank.available else None,
        "availability_text": bank.text,
        "availability_badge": bank.badge,
    }


def with_availability(row: Mapping[str, object]) -> dict[str, object]:
    """`row` (a hub or bank read-model dict carrying the raw `availability`/`availability_reason` columns,
    or not) with the four API fields of `availability_fields`."""
    out = dict(row)
    raw_availability = out.pop("availability", None)
    raw_reason = out.pop("availability_reason", None)
    out.update(
        availability_fields(
            raw_availability if isinstance(raw_availability, str) else None,
            raw_reason if isinstance(raw_reason, str) else None,
        )
    )
    return out


def contract_labels(contract: Contract) -> dict[str, object]:
    """A contract as the operator/customer APIs list it (D-37): its fields plus `label` ("SAMPLE -
    INACTIVE" for a sample contract, else None). A sample contract means there is no real contract for its
    utility, so it also carries the REGULATED_NO_CONTRACT availability fields."""
    out: dict[str, object] = contract.model_dump(mode="json")
    out["label"] = SAMPLE_INACTIVE_LABEL if contract.is_sample else None
    if contract.is_sample:
        out.update(availability_fields(UNAVAILABLE, REGULATED_NO_CONTRACT))
    return out


def unavailable_bank_ids(banks: Iterable[BankAvailability]) -> frozenset[str]:
    return frozenset(b.bank_id for b in banks if not b.available)


def is_grandfathered(obligation_created_at: datetime | None, bank: BankAvailability) -> bool:
    """The time half of the K13 grandfather rule: the obligation predates the bank becoming unavailable.
    Missing data is never grandfathered (the caller then keeps the obligation off the bank)."""
    if bank.available or bank.since is None or obligation_created_at is None:
        return False
    return obligation_created_at < bank.since


def grandfathered_banks_by_obligation(pairs: Iterable[tuple[str, str]]) -> dict[str, frozenset[str]]:
    """`{obligation_id: banks}` from `GRANDFATHERED_SQL` rows `(obligation_id, bank_id)`."""
    out: dict[str, set[str]] = {}
    for obligation_id, bank_id in pairs:
        out.setdefault(str(obligation_id), set()).add(str(bank_id))
    return {k: frozenset(v) for k, v in out.items()}


def available_kw(kw_by_bank: Mapping[str, float], unavailable: frozenset[str]) -> tuple[float, float]:
    """Split a per-bank kW figure into (available, unavailable) totals: the capacity tiles show the
    unavailable part on its own line ("Regulated market - no contract: N kW"), never in available kW."""
    avail = sum(kw for bank_id, kw in kw_by_bank.items() if bank_id not in unavailable)
    return avail, sum(kw for bank_id, kw in kw_by_bank.items() if bank_id in unavailable)


#: The grandfather rule (module docstring) as SQL: `(obligation_id, bank_id)` pairs. Small: it starts from
#: the unavailable banks (ix_bank_unavailable) and their live reservations (ix_reservation_bank_interval).
GRANDFATHERED_SQL: Final = """
SELECT DISTINCT r.obligation_id::text, r.bank_id
FROM og.bank b
JOIN og.reservation r ON r.bank_id = b.bank_id AND r.released_at IS NULL
JOIN og.obligation o ON o.obligation_id = r.obligation_id
WHERE b.availability = 'UNAVAILABLE'
  AND b.availability_since IS NOT NULL
  AND o.created_at < b.availability_since
  AND o.state IN ('COMMITTED', 'DELIVERING', 'SHORTFALL')
  AND o.window_end > now()
"""

#: `(bank_id, availability, availability_reason, availability_since)` for every bank.
BANK_AVAILABILITY_SQL: Final = (
    "SELECT bank_id, availability, availability_reason, availability_since FROM og.bank ORDER BY bank_id"
)

__all__ = [
    "BANK_AVAILABILITY_SQL",
    "GRANDFATHERED_SQL",
    "R_BANK_UNAVAILABLE",
    "BankAvailability",
    "availability_badge",
    "availability_fields",
    "availability_text",
    "available_kw",
    "contract_labels",
    "grandfathered_banks_by_obligation",
    "is_grandfathered",
    "parse_availability",
    "unavailable_bank_ids",
    "with_availability",
]
