"""Product-rule quantity rounding: min_qty / increment / block -> variable-kind mapping and rounding.

Single owner per 02a S3.6 / 02b S12. `selector` builds LP/MILP variables from this; `contracts` admission
uses the same rounding for an early infeasibility check.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Literal

VariableKind = Literal["CONTINUOUS", "SEMI_CONTINUOUS", "BINARY"]

_EPSILON = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class ProductRule:
    min_qty_kw: Decimal
    increment_kw: Decimal
    block: bool


def derive_variable_kind(rule: ProductRule) -> VariableKind:
    """block -> BINARY; min_qty>0 or increment>epsilon -> SEMI_CONTINUOUS; else CONTINUOUS.
    Mirrors product_rule.variable_kind's admission-time derivation (02a S1.3)."""
    if rule.block:
        return "BINARY"
    if rule.min_qty_kw > 0 or rule.increment_kw > _EPSILON:
        return "SEMI_CONTINUOUS"
    return "CONTINUOUS"


def round_quantity(requested_kw: Decimal, rule: ProductRule, max_kw: Decimal) -> Decimal:
    """Round a requested quantity down to a feasible tradable quantity under the product's rule.
    Returns Decimal(0) if the request cannot meet min_qty_kw within max_kw (an infeasible ask).
    """
    if requested_kw <= 0:
        return Decimal(0)
    capped = min(requested_kw, max_kw)
    kind = derive_variable_kind(rule)

    if kind == "BINARY":
        # all-or-nothing: only representable quantity is 0 or max_kw (or min_qty_kw if it's the
        # contractual block size and <= max_kw).
        block_kw = rule.min_qty_kw if rule.min_qty_kw > 0 else max_kw
        return block_kw if capped >= block_kw else Decimal(0)

    if kind == "CONTINUOUS":
        return capped

    # SEMI_CONTINUOUS: 0, or min_qty_kw + n*increment_kw <= capped
    if capped < rule.min_qty_kw:
        return Decimal(0)
    if rule.increment_kw <= 0:
        return capped
    steps = ((capped - rule.min_qty_kw) / rule.increment_kw).to_integral_value(rounding=ROUND_DOWN)
    return rule.min_qty_kw + steps * rule.increment_kw


def is_feasible(requested_kw: Decimal, rule: ProductRule, max_kw: Decimal) -> bool:
    """Admission-time pre-check: can this request ever be non-zero under round_quantity?"""
    return round_quantity(requested_kw, rule, max_kw) > 0
