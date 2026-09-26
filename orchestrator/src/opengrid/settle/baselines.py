"""M&V baselines per MVP-S service (02a S7.1).

| Service            | Baseline                                                          |
|--------------------|--------------------------------------------------------------------|
| HOME               | none -- reserve integrity only, no invoice line                    |
| ERCOT_ENERGY       | ALR set-point tracking vs. simulated UDSP                           |
| ERCOT_AS           | award x MCPC; hold compliance (SOC >= H_k r/eta_d)                   |
| DIST_DEFERRAL      | SCADA_OUTCOME -- bank apparent power <= limit for the need-window    |
| PARTNER_CAPACITY   | DIRECT_HUB_METER -- event-average delivered vs. committed            |
| DATA_CENTER        | AMI_INTERVAL -- site-meter kW vs. committed bridging capacity (S4.b) |
| PIPELINE_AC        | DIRECT_HUB_METER -- corridor-mitigation dispatch metered at the hub (06 S4.a) |
| REGULATED_CAPACITY | DIRECT_HUB_METER -- a utility capacity hold, metered like ERCOT_AS (08 S3) |
| PJM_CAPACITY       | DIRECT_HUB_METER -- same ISO-capacity-hold shape as ERCOT_AS (SERVICES agent, 2026-09-26) |
| MOBILE_STORAGE     | AMI_INTERVAL -- site-meter-scoped deployment, like DATA_CENTER (SERVICES agent, 2026-09-26) |
| LARGE_LOAD         | DIRECT_HUB_METER -- an event-driven curtailment call, metered like PARTNER_CAPACITY (SERVICES agent, 2026-09-26) |

MVP-S simplification (documented, not a spec deviation): for every non-HOME service the baseline
target for one interval is the obligation's committed capacity held for that interval's duration --
`K_{o,j}` in 02a S7.2's compliance formula `C_{o,j} = D_{o,j} / K_{o,j}`. The service-specific
*method* (which meter source feeds `D_{o,j}`) is selected by `metering.meter_interval`'s `source`
argument, chosen by the caller from this table.

`PJM_CAPACITY`/`MOBILE_STORAGE`/`LARGE_LOAD` come from `opengrid.settle.services_extra.
EXTRA_METER_SOURCE_BY_SERVICE` (the SERVICES agent's pre-cleared addition; see that module's
docstring for the full registration picture, including `pjm_non_performance_charge`).
`PIPELINE_AC`/`REGULATED_CAPACITY` are this module's own entries. `opengrid.settle.__init__` indexes
this dict directly for every `ServiceType` value with no fallback, so a missing entry is a `KeyError`
at settlement time, not a graceful default; `test_baselines.py::test_every_service_type_has_a_meter_source`
guards against that.
"""

from __future__ import annotations

from decimal import Decimal
from typing import get_args

from opengrid.core.models.engine import ServiceType
from opengrid.settle.models import MeterSource
from opengrid.settle.services_extra import EXTRA_METER_SOURCE_BY_SERVICE

METER_SOURCE_BY_SERVICE: dict[ServiceType, MeterSource] = {
    "HOME": "AMI_INTERVAL",
    "ERCOT_ENERGY": "DIRECT_HUB_METER",
    "ERCOT_AS": "DIRECT_HUB_METER",
    "DIST_DEFERRAL": "SCADA_OUTCOME",
    "PARTNER_CAPACITY": "DIRECT_HUB_METER",
    "DATA_CENTER": "AMI_INTERVAL",
    "PIPELINE_AC": "DIRECT_HUB_METER",
    "REGULATED_CAPACITY": "DIRECT_HUB_METER",
    # PJM_CAPACITY, MOBILE_STORAGE, LARGE_LOAD: the SERVICES agent's canonical sources
    # (opengrid.settle.services_extra.EXTRA_METER_SOURCE_BY_SERVICE's own docstring reasoning --
    # MOBILE_STORAGE is AMI_INTERVAL, site-meter-scoped like DATA_CENTER, not hub-metered).
    **EXTRA_METER_SOURCE_BY_SERVICE,
}
if set(METER_SOURCE_BY_SERVICE) != set(get_args(ServiceType)):
    # Fail at import time, not at the first settle() call for the missing service (BUILD.md S5a "no
    # silent fallbacks") -- also guarded by test_baselines.py::test_every_service_type_has_a_meter_source.
    raise RuntimeError(
        "METER_SOURCE_BY_SERVICE must cover every ServiceType -- settle.__init__ indexes it directly "
        "with no fallback, so a missing entry is a live KeyError, not a graceful default"
    )


def has_baseline(service_type: ServiceType) -> bool:
    """HOME has no M&V baseline and posts no invoice line (02a S7.1/S7.3)."""
    return service_type != "HOME"


def compute_baseline_kwh(
    service_type: ServiceType,
    committed_kw: Decimal,
    duration_hours: Decimal,
) -> Decimal | None:
    """K_{o,j}: the committed-capacity target for this interval. `None` for `HOME` (02a S7.1)."""
    if not has_baseline(service_type):
        return None
    return committed_kw * duration_hours
