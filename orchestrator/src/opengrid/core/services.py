"""Service-type classes shared across the engine, guardian and invariants (one owner, BUILD.md S1).

`og.obligation.service_type` values are declared in `opengrid.core.models.engine.ServiceType`; this module
holds the groupings of them that more than one component decides on.
"""

from __future__ import annotations

from typing import Final

#: An ERCOT ancillary-service award (NPRR1282).
ERCOT_AS_SERVICE_TYPE: Final = "ERCOT_AS"
#: A regulated-utility capacity service, incl. the utility toll (D-29, variant TOLLING).
REGULATED_CAPACITY_SERVICE_TYPE: Final = "REGULATED_CAPACITY"

#: Capacity holds: granted 0 kW with R-GRANT-AS-HOLD and the reservation locked until an `og.as_deployment`
#: covers the obligation, then delivered up to `committed_kw`. The allocator holds them, the guardian's G-19
#: corroborates the hold, and the K13 invariant floors them at 0 kW outside a deployment.
HOLD_SERVICE_TYPES: Final[frozenset[str]] = frozenset(
    {ERCOT_AS_SERVICE_TYPE, REGULATED_CAPACITY_SERVICE_TYPE}
)
