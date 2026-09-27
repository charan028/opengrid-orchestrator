"""Settlement rules for PJM_CAPACITY, MOBILE_STORAGE and LARGE_LOAD (owner decision 2026-09-26).

Spec: `docs/orchestrator/07-delivery/14-additional-services.md`, `config/service_profiles/
{pjm_capacity,mobile_storage,large_load}.toml`. Written as a NEW module, per the SERVICES agent's
ownership grant (BUILD.md ownership map, 2026-09-26 13:30 CT): `opengrid.settle` is owned end-to-end
by the settle agent, but this file was pre-cleared as an addition so PJM_CAPACITY/MOBILE_STORAGE/
LARGE_LOAD settlement math ships alongside their profiles without a second round-trip. It follows the
same rules as every other module in this package (`opengrid.settle.README`): pure functions, no I/O,
no import of `opengrid.platform.db`.

Registration wiring the settle owner still needs to apply (not done here -- this module only owns
the pure math, never `opengrid.settle`'s registries or orchestration):

1. `opengrid/settle/baselines.py`: `METER_SOURCE_BY_SERVICE` is a closed `dict[ServiceType, MeterSource]`
   keyed by every service type `settle()` meters (`METER_SOURCE_BY_SERVICE[ctx.service_type]`, no
   `.get()` fallback) -- add the three entries below, e.g.
   `METER_SOURCE_BY_SERVICE.update(EXTRA_METER_SOURCE_BY_SERVICE)` right after the dict literal, or
   inline the three keys directly. Without this, `settle()` raises `KeyError` for any obligation on
   one of these three service types. `tests/unit/profiles/test_data_center_registered.py`'s
   `set(METER_SOURCE_BY_SERVICE) == set(get_args(ServiceType))` assertion already fails until this
   lands (migration 0025 widened `ServiceType` first).
2. `opengrid/settle/billing.py`: no change is required for the baseline `CAPACITY_PAYMENT` line --
   `draft_invoice_lines`'s existing `else` branch (committed_kwh * price_per_kwh * performance_factor)
   already covers any service type it does not special-case, which already covers all three new types
   correctly for routine (non-emergency-hour) intervals. To bill PJM's non-performance charge, have
   `opengrid.settle.settle()` call `pjm_non_performance_charge()` below when `ctx.service_type ==
   "PJM_CAPACITY"` and the interval falls inside a declared emergency performance hour (a new
   `SettleBackend` accessor for that flag/window is settle's to add), and append the result as an
   additional `InvoiceLineDraft(line_type="LD_PENALTY", ...)` alongside `draft_invoice_lines`'s output
   -- never in place of it, since the ordinary capacity payment still applies.

See also (other owners, not settle -- listed here only so this module's callers know the full wiring
picture; SERVICES cannot edit these paths):

- `opengrid/selector/gate.py`'s `_CATEGORY_BY_SERVICE_TYPE` needs `"PJM_CAPACITY": "MARKET"` (non-firm
  by default, 03 S2.6), `"MOBILE_STORAGE": "FIRM"`, `"LARGE_LOAD": "FIRM"` (optimizer owner). Until
  added, `.get(row["service_type"], "MARKET")`'s fallback silently treats all three as `MARKET`
  priority, which is only correct for `PJM_CAPACITY`.
"""

from __future__ import annotations

from decimal import Decimal

from opengrid.core.models.engine import ServiceType
from opengrid.core.services import (
    LARGE_LOAD_SERVICE_TYPE,
    MOBILE_STORAGE_SERVICE_TYPE,
    PJM_CAPACITY_SERVICE_TYPE,
)
from opengrid.settle.models import MeterSource

#: `opengrid.settle.baselines.METER_SOURCE_BY_SERVICE`'s missing keys for the three new service types
#: (see module docstring item 1). `PJM_CAPACITY`/`LARGE_LOAD` meter at the hub like `DATA_CENTER`/
#: `ERCOT_AS`; `MOBILE_STORAGE` meters at the deployment's site meter like `DATA_CENTER` (S1.1: both are
#: `SITE_METER`-scoped in their `ServiceProfile.target_scope`).
EXTRA_METER_SOURCE_BY_SERVICE: dict[ServiceType, MeterSource] = {
    PJM_CAPACITY_SERVICE_TYPE: "DIRECT_HUB_METER",
    MOBILE_STORAGE_SERVICE_TYPE: "AMI_INTERVAL",
    LARGE_LOAD_SERVICE_TYPE: "DIRECT_HUB_METER",
}

#: numeric(14,6)/numeric(18,6) column precision (matches `opengrid.settle`'s own `_KWH_EPSILON`/
#: `_MONEY_EPSILON`) -- a dip that is only a rounding artifact must never be billed as a shortfall.
_KWH_EPSILON = Decimal("0.000001")


def pjm_non_performance_charge(
    committed_kw: Decimal,
    delivered_kw: Decimal,
    duration_hours: Decimal,
    non_performance_rate_per_kwh: Decimal,
    *,
    is_emergency_performance_hour: bool,
) -> Decimal:
    """Simulated PJM Capacity Performance non-performance charge (14-additional-services.md).

    Real PJM prices non-performance against Net CONE and the Balancing Ratio over a rolling 40-hour
    window; this orchestrator has no PJM membership, capacity-market registration or settlement feed
    (PJM stays simulated per the owner's 2026-09-26 decision), so a single configured
    `non_performance_rate_per_kwh` stands in for that market calculation rather than reproducing it.

    Charged only for a shortfall DURING a declared emergency performance hour -- every other interval
    settles through the ordinary `CAPACITY_PAYMENT x performance_factor` line
    (`opengrid.settle.billing.draft_invoice_lines`'s generic branch) and is never charged here too.
    Never negative: an over-delivery is not a rebate through this line."""
    if not is_emergency_performance_hour:
        return Decimal("0")
    committed_kwh = committed_kw * duration_hours
    delivered_kwh = delivered_kw * duration_hours
    shortfall_kwh = max(Decimal("0"), committed_kwh - delivered_kwh)
    if shortfall_kwh <= _KWH_EPSILON:
        return Decimal("0")
    return shortfall_kwh * non_performance_rate_per_kwh


def mobile_deployment_availability_pct(
    energized_minutes: int,
    window_minutes: int,
) -> Decimal | None:
    """Share of the deployment window the mobile unit was on-site, energized and within its compliance
    band (`mobile_storage.toml`'s `deployment_availability_pct` settlement line -- the MOBILE_STORAGE
    analogue of DATA_CENTER's `pq_compliance_pct`, S4.b). `None` when the window has zero minutes
    (nothing to measure, mirrors `performance.compute_compliance_pct`'s `None`-on-no-baseline rule).
    Clipped to `[0, 1]`: a window that somehow reports more energized minutes than its own length (a
    stale or overlapping reading) is reported as fully available, not over 100%."""
    if window_minutes <= 0:
        return None
    fraction = Decimal(energized_minutes) / Decimal(window_minutes)
    return min(Decimal("1"), max(Decimal("0"), fraction))


def large_load_curtailment_compliance_pct(
    delivered_kw: Decimal,
    scheduled_kw: Decimal,
) -> Decimal | None:
    """Share of the scheduled load-following/ride-through target actually delivered for one interval
    (`large_load.toml`'s `capacity_payment_x_performance` line). LARGE_LOAD has no PQ compliance line
    (S4.b's explicit grid-code-minimum contrast with DATA_CENTER) -- this is a kW-only figure. `None`
    when nothing was scheduled for the interval (no event in progress, nothing to measure)."""
    if scheduled_kw <= 0:
        return None
    return max(Decimal("0"), delivered_kw / scheduled_kw)
