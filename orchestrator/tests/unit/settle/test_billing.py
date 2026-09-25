"""TS-08-04: insert-only invoice lines, and 02a S7.3's line_type mapping per service."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

from opengrid.settle.billing import draft_invoice_lines, next_version
from opengrid.settle.models import InvoiceLineDraft


@dataclass(frozen=True, slots=True)
class _ExistingLine:
    invoice_line_id: object
    amount: Decimal
    version: int


def test_home_posts_no_invoice_lines():
    lines = draft_invoice_lines(
        service_type="HOME",
        delivered_kwh=Decimal("10"),
        price_per_kwh=Decimal("0.10"),
        revenue=Decimal("1.00"),
        performance_factor=Decimal("1"),
        penalty_amount=Decimal("0"),
    )
    assert lines == []


def test_ercot_energy_posts_one_energy_line():
    lines = draft_invoice_lines(
        service_type="ERCOT_ENERGY",
        delivered_kwh=Decimal("100"),
        price_per_kwh=Decimal("0.10"),
        revenue=Decimal("10.00"),
        performance_factor=Decimal("1"),
        penalty_amount=Decimal("0"),
    )
    assert len(lines) == 1
    assert lines[0].line_type == "ENERGY"
    assert lines[0].amount == Decimal("10.00")


def test_dist_deferral_capacity_payment_scaled_by_performance_factor():
    """Hand computation: revenue 10.00 * performance_factor 0.8 = 8.00 (02a S7.3: "x performance
    factor")."""
    lines = draft_invoice_lines(
        service_type="DIST_DEFERRAL",
        delivered_kwh=Decimal("100"),
        price_per_kwh=Decimal("0.10"),
        revenue=Decimal("10.00"),
        performance_factor=Decimal("0.8"),
        penalty_amount=Decimal("0"),
    )
    assert len(lines) == 1
    assert lines[0].line_type == "CAPACITY_PAYMENT"
    assert lines[0].amount == Decimal("8.00")


def test_ld_penalty_line_added_when_penalty_positive():
    lines = draft_invoice_lines(
        service_type="ERCOT_AS",
        delivered_kwh=Decimal("100"),
        price_per_kwh=Decimal("0.10"),
        revenue=Decimal("10.00"),
        performance_factor=Decimal("1"),
        penalty_amount=Decimal("2.50"),
    )
    line_types = [line.line_type for line in lines]
    assert "CAPACITY_PAYMENT" in line_types
    assert "LD_PENALTY" in line_types
    ld_penalty = next(line for line in lines if line.line_type == "LD_PENALTY")
    assert ld_penalty.amount == Decimal("-2.50")


def test_next_version_first_insert_is_version_1_no_supersedes():
    draft = InvoiceLineDraft(
        line_type="ENERGY", quantity=Decimal("100"), unit="kWh", rate=Decimal("0.10"), amount=Decimal("10.00")
    )
    decision = next_version(draft, None, quality_flag="GOOD")
    assert decision is not None
    _, version, supersedes, status = decision
    assert version == 1
    assert supersedes is None
    assert status == "FINAL"


def test_next_version_estimated_quality_never_reaches_final():
    """TS-08-09: an estimated interval's invoice line is never marked FINAL."""
    draft = InvoiceLineDraft(
        line_type="ENERGY", quantity=Decimal("100"), unit="kWh", rate=Decimal("0.10"), amount=Decimal("10.00")
    )
    decision = next_version(draft, None, quality_flag="ESTIMATED")
    assert decision is not None
    _, _version, _supersedes, status = decision
    assert status == "PROVISIONAL"


def test_next_version_unchanged_amount_is_idempotent_noop():
    """Re-running settlement over an unchanged interval must not duplicate the invoice line
    (BUILD.md: "re-running a period produces no duplicates")."""
    existing = _ExistingLine(invoice_line_id=uuid4(), amount=Decimal("10.00"), version=1)
    draft = InvoiceLineDraft(
        line_type="ENERGY", quantity=Decimal("100"), unit="kWh", rate=Decimal("0.10"), amount=Decimal("10.00")
    )

    assert next_version(draft, existing, quality_flag="GOOD") is None


def test_next_version_changed_amount_is_an_insert_only_correction():
    """TS-08-04: a settlement correction is a new, insert-only versioned row referencing the
    original -- never an UPDATE of the original."""
    original_id = uuid4()
    existing = _ExistingLine(invoice_line_id=original_id, amount=Decimal("10.00"), version=1)
    draft = InvoiceLineDraft(
        line_type="ENERGY", quantity=Decimal("110"), unit="kWh", rate=Decimal("0.10"), amount=Decimal("11.00")
    )

    decision = next_version(draft, existing, quality_flag="GOOD")
    assert decision is not None
    new_draft, version, supersedes, status = decision
    assert new_draft.amount == Decimal("11.00")
    assert version == 2
    assert supersedes == original_id
    assert status == "CORRECTED"
