"""Regulated capacity payment (08 S1 "regulated utilities pay a premium for capacity", S3b; 09 S1.5 stage R).

    payment = committed_kW x annual price ($/kW-yr) x hours / 8760 x performance factor

A $/kW-month price is annualised as x 12 first, so both bases pro-rate identically by the hours in the
period (a 24 h gate, a settlement month or a year). The performance factor is settle's
delivered/committed compliance, capped to [0, 1] (09 OQ-3: PF = delivered/committed at the meter/POI,
net of charging). Pure.
"""

from __future__ import annotations

from decimal import Decimal

from opengrid.core.models.market import CapacityPaymentBasis

HOURS_PER_YEAR = Decimal("8760")
_MONTHS_PER_YEAR = Decimal("12")
_ZERO = Decimal("0")
_ONE = Decimal("1")


def annual_capacity_price(price_usd_per_kw: Decimal, basis: CapacityPaymentBasis) -> Decimal:
    """The contract price as $/kW-yr."""
    if price_usd_per_kw < _ZERO:
        raise ValueError(f"capacity price must be >= 0, got {price_usd_per_kw}")
    return price_usd_per_kw * _MONTHS_PER_YEAR if basis == "USD_PER_KW_MONTH" else price_usd_per_kw


def regulated_capacity_payment(
    *,
    committed_kw: Decimal,
    price_usd_per_kw: Decimal,
    basis: CapacityPaymentBasis,
    hours: Decimal,
    performance_factor: Decimal = _ONE,
) -> Decimal:
    """The capacity payment for `committed_kw` held over `hours`. `performance_factor` is clamped to
    [0, 1]: over-delivery never earns more than the committed payment."""
    if committed_kw < _ZERO or hours < _ZERO:
        raise ValueError("committed_kw and hours must be >= 0")
    pf = min(max(performance_factor, _ZERO), _ONE)
    return committed_kw * annual_capacity_price(price_usd_per_kw, basis) * hours / HOURS_PER_YEAR * pf
