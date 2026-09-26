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
    committed_kwh: Decimal,
    price_per_kwh: Decimal,
    revenue: Decimal,
    performance_factor: Decimal,
    penalty_amount: Decimal,
) -> list[InvoiceLineDraft]:
    """Which invoice lines this obligation-interval posts, per the table above. `performance_factor`
    is `performance.compute_compliance_pct`'s result (or 1 if there is none), used to scale
    `CAPACITY_PAYMENT` per 02a S7.3's "x performance factor" rule.

    `CAPACITY_PAYMENT` for `PARTNER_CAPACITY`/`DIST_DEFERRAL` is `committed_kwh * price_per_kwh *
    performance_factor` -- committed capacity valued at the contract price, scaled once by
    performance. It is deliberately *not* `revenue * performance_factor`: `revenue` (`pnl.revenue`,
    profitability.py) is already `price_per_kwh * delivered_kwh`, and `delivered_kwh` already
    reflects any shortfall, so multiplying that by `performance_factor` again double-counts the
    shortfall (e.g. 80% delivery would bill ~64% instead of 80% of the committed payment).

    `ERCOT_AS` is deliberately its OWN branch, never scaled by `performance_factor`: an AS award pays
    for the capacity HELD (`award x MCPC`, per the table above), not for energy delivered, so
    `performance_factor` (delivered/committed) would otherwise near-zero the payment on a normal
    hold-not-discharge interval (2026-09-26 live-soak correction, alongside `profitability.
    compute_revenue`'s matching fix for `pnl.revenue`)."""
    if service_type == "HOME":
        return []

    if service_type == "ERCOT_ENERGY":
        lines = [
            InvoiceLineDraft(
                line_type="ENERGY", quantity=delivered_kwh, unit="kWh", rate=price_per_kwh, amount=revenue
            )
        ]
    elif service_type == "ERCOT_AS":
        lines = [
            InvoiceLineDraft(
                line_type="CAPACITY_PAYMENT",
                quantity=committed_kwh,
                unit="kWh",
                rate=price_per_kwh,
                amount=committed_kwh * price_per_kwh,
            )
        ]
    else:
        capacity_amount = committed_kwh * price_per_kwh * performance_factor
        lines = [
            InvoiceLineDraft(
                line_type="CAPACITY_PAYMENT",
                quantity=committed_kwh,
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
