"""Insert-only invoice-line rules (02a S7.3).

| Service          | `line_type`s posted                                                        |
|------------------|-----------------------------------------------------------------------------|
| ERCOT_ENERGY     | `ENERGY` (settlement-shadow value)                                          |
| ERCOT_AS         | `CAPACITY_PAYMENT` (award x MCPC), `LD_PENALTY` if a deployment failed       |
| DIST_DEFERRAL    | `CAPACITY_PAYMENT` x performance factor, `LD_PENALTY` for a failed interval  |
| PARTNER_CAPACITY | `CAPACITY_PAYMENT` x performance factor (EVENT)                             |
| HOME             | none                                                                          |

`draft_invoice_lines` is pure (given the interval's numbers, decide *what* to post); `next_version`
is the insert-only correction rule (02a S1.1: "never UPDATE, only INSERT ... a `supersedes` column
for corrections") -- it never mutates anything, it only decides whether a new row is needed and, if
so, what version/status/`supersedes` it carries.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol
from uuid import UUID

from opengrid.core.models.engine import ServiceType
from opengrid.settle.models import InvoiceLineDraft, QualityFlag

_AMOUNT_EPSILON = Decimal("0.000001")  # numeric(18,6) column precision


def draft_invoice_lines(
    *,
    service_type: ServiceType,
    delivered_kwh: Decimal,
    price_per_kwh: Decimal,
    revenue: Decimal,
    performance_factor: Decimal,
    penalty_amount: Decimal,
) -> list[InvoiceLineDraft]:
    """Which invoice lines this obligation-interval posts, per the table above. `performance_factor`
    is `performance.compute_compliance_pct`'s result (or 1 if there is none), used to scale
    `CAPACITY_PAYMENT` per 02a S7.3's "x performance factor" rule."""
    if service_type == "HOME":
        return []

    if service_type == "ERCOT_ENERGY":
        lines = [
            InvoiceLineDraft(
                line_type="ENERGY", quantity=delivered_kwh, unit="kWh", rate=price_per_kwh, amount=revenue
            )
        ]
    else:
        capacity_amount = revenue * performance_factor
        lines = [
            InvoiceLineDraft(
                line_type="CAPACITY_PAYMENT",
                quantity=delivered_kwh,
                unit="kWh",
                rate=price_per_kwh,
                amount=capacity_amount,
            )
        ]

    if penalty_amount > 0:
        lines.append(
            InvoiceLineDraft(
                line_type="LD_PENALTY",
                quantity=delivered_kwh,
                unit="kWh",
                rate=None,
                amount=-penalty_amount,
            )
        )
    return lines


class ExistingInvoiceLine(Protocol):
    """The subset of an `og.invoice_line` row `next_version` needs to decide on a correction.

    Declared with read-only `@property` members (not plain attributes) so frozen dataclasses --
    `og.invoice_line` rows are never mutated (02a S1.1) -- satisfy this Protocol structurally; mypy
    treats a frozen dataclass's fields as read-only and a plain Protocol attribute as read-write.
    """

    @property
    def invoice_line_id(self) -> UUID: ...
    @property
    def amount(self) -> Decimal: ...
    @property
    def version(self) -> int: ...


def next_version(
    draft: InvoiceLineDraft,
    existing: ExistingInvoiceLine | None,
    *,
    quality_flag: QualityFlag,
) -> tuple[InvoiceLineDraft, int, UUID | None, str] | None:
    """Decide the insert to make for `draft` given the currently-active line (if any).

    Returns `(draft, version, supersedes, status)` to insert, or `None` if re-running settlement for
    an unchanged interval should be a no-op (idempotency: re-running a period produces no
    duplicates). A changed amount is a correction: new version, `supersedes` the prior line, status
    `CORRECTED`. `quality_flag != "GOOD"` (an estimated/disputed interval) never reaches `FINAL`
    (02a S7.2, TS-08-09), so it always posts (or re-posts) as `PROVISIONAL`.
    """
    status = "FINAL" if quality_flag == "GOOD" else "PROVISIONAL"

    if existing is None:
        return draft, 1, None, status

    if abs(existing.amount - draft.amount) < _AMOUNT_EPSILON and status != "CORRECTED":
        return None  # idempotent re-run: identical inputs, nothing to insert

    return draft, existing.version + 1, existing.invoice_line_id, "CORRECTED"
