"""M&V baselines per MVP-S service (02a S7.1).

| Service            | Baseline                                                          |
|--------------------|--------------------------------------------------------------------|
| HOME               | none -- reserve integrity only, no invoice line                    |
| ERCOT_ENERGY       | ALR set-point tracking vs. simulated UDSP                           |
| ERCOT_AS           | award x MCPC; hold compliance (SOC >= H_k r/eta_d)                   |
| DIST_DEFERRAL      | SCADA_OUTCOME -- bank apparent power <= limit for the need-window    |
| PARTNER_CAPACITY   | DIRECT_HUB_METER -- event-average delivered vs. committed            |

MVP-S simplification (documented, not a spec deviation): for every non-HOME service the baseline
target for one interval is the obligation's committed capacity held for that interval's duration --
`K_{o,j}` in 02a S7.2's compliance formula `C_{o,j} = D_{o,j} / K_{o,j}`. The service-specific
*method* (which meter source feeds `D_{o,j}`) is selected by `metering.meter_interval`'s `source`
argument, chosen by the caller from this table.
"""

from __future__ import annotations

from decimal import Decimal

from opengrid.core.models.engine import ServiceType
from opengrid.settle.models import MeterSource

METER_SOURCE_BY_SERVICE: dict[ServiceType, MeterSource] = {
    "HOME": "AMI_INTERVAL",
    "ERCOT_ENERGY": "DIRECT_HUB_METER",
    "ERCOT_AS": "DIRECT_HUB_METER",
    "DIST_DEFERRAL": "SCADA_OUTCOME",
    "PARTNER_CAPACITY": "DIRECT_HUB_METER",
}


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
