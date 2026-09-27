"""`og.obligation.service_type` / `og.contract.service_type` values and the groupings of them that more than one
component decides on -- ONE definition each (BUILD.md S1, one owner per function). Every module that compares a
service type imports its constant from here; the `ServiceType` Literal in `opengrid.core.models.engine` (the
og.contract CHECK, migration 0025) lists the same values and `tests/unit/core/test_services.py` keeps the two
in step.
"""

from __future__ import annotations

from typing import Final

#: A residential homeowner program (tariff/VPP enrollment).
HOME_SERVICE_TYPE: Final = "HOME"
#: ERCOT energy arbitrage.
ERCOT_ENERGY_SERVICE_TYPE: Final = "ERCOT_ENERGY"
#: An ERCOT ancillary-service award (NPRR1282).
ERCOT_AS_SERVICE_TYPE: Final = "ERCOT_AS"
#: A distribution-deferral (non-wires) call from a utility.
DIST_DEFERRAL_SERVICE_TYPE: Final = "DIST_DEFERRAL"
#: A partner's capacity contract.
PARTNER_CAPACITY_SERVICE_TYPE: Final = "PARTNER_CAPACITY"
#: Firm bridging capacity for a data center (06-service-profiles S4.b; closed-loop, PQ-sensitive).
DATA_CENTER_SERVICE_TYPE: Final = "DATA_CENTER"
#: Pipeline AC-interference mitigation (closed-loop, PQ-sensitive).
PIPELINE_AC_SERVICE_TYPE: Final = "PIPELINE_AC"
#: A regulated utility's capacity contract, incl. the utility toll (08 S3; D-29, variant TOLLING).
REGULATED_CAPACITY_SERVICE_TYPE: Final = "REGULATED_CAPACITY"
#: A (simulated) PJM capacity-market commitment.
PJM_CAPACITY_SERVICE_TYPE: Final = "PJM_CAPACITY"
#: A D-31 mobile storage (truck) service.
MOBILE_STORAGE_SERVICE_TYPE: Final = "MOBILE_STORAGE"
#: A large-load (flexible demand) service.
LARGE_LOAD_SERVICE_TYPE: Final = "LARGE_LOAD"

#: Every service type above, in the og.contract CHECK's order.
ALL_SERVICE_TYPES: Final[tuple[str, ...]] = (
    HOME_SERVICE_TYPE,
    ERCOT_ENERGY_SERVICE_TYPE,
    ERCOT_AS_SERVICE_TYPE,
    DIST_DEFERRAL_SERVICE_TYPE,
    PARTNER_CAPACITY_SERVICE_TYPE,
    DATA_CENTER_SERVICE_TYPE,
    PIPELINE_AC_SERVICE_TYPE,
    REGULATED_CAPACITY_SERVICE_TYPE,
    PJM_CAPACITY_SERVICE_TYPE,
    MOBILE_STORAGE_SERVICE_TYPE,
    LARGE_LOAD_SERVICE_TYPE,
)

#: Capacity holds: granted 0 kW with R-GRANT-AS-HOLD and the reservation locked until an `og.as_deployment`
#: covers the obligation, then delivered up to `committed_kw`. The allocator holds them, the guardian's G-19
#: corroborates the hold, the K13 invariant floors them at 0 kW outside a deployment, and settlement pays them
#: as capacity.
HOLD_SERVICE_TYPES: Final[frozenset[str]] = frozenset(
    {ERCOT_AS_SERVICE_TYPE, REGULATED_CAPACITY_SERVICE_TYPE}
)
