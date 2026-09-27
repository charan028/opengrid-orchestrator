"""ERCOT AS deployment instructions served by ogsim.market (D-35).

ogsim.market mounts the MMS/EWS simulator (`ogsim.protocols.ercot_mms`) at `/mms`, so the running market
sim answers `POST /mms/ews/` like ERCOT's EWS endpoint. Its dispatch-instruction book is fed by market
anomalies injected through the control plane (manual, scenario YAML). Those anomalies are never drawn by
random mode unless `config/random.yaml` lists them, and it does not.

| anomaly type             | what ERCOT sends                                                              |
|--------------------------|-------------------------------------------------------------------------------|
| `ercot_as_deploy`        | DEPLOY_AS for `target` (resource): service, MW (0 = the award), ramp, duration |
| `ercot_as_recall`        | RECALL_AS ending the newest deployment of that resource and service            |
| `ercot_as_duplicate`     | a DEPLOY_AS that is delivered again after the QSE acknowledged it              |
| `ercot_as_out_of_order`  | a DEPLOY_AS and its RECALL_AS, the recall visible first                        |
| `ercot_as_malformed`     | a DEPLOY_AS whose MW and start time are not parseable                          |
| `ercot_as_unknown_award` | a DEPLOY_AS for a resource/service with no award (default `OG_ESR_UNKNOWN`)    |
| `ercot_as_exceed_award`  | a DEPLOY_AS for `factor` x the award's MW                                      |

Awards the sim knows (for the default MW) come from `OGSIM_AS_AWARDS`: comma-separated
`RESOURCE:SERVICE:MW`, default `OG_ESR_1:ECRS:0.5,OG_ESR_1:RRS:0.5`. Delivery realism comes from
`OGSIM_AS_LATENCY_S` (`low,high`, default `1,4`), `OGSIM_AS_DUPLICATE_PROBABILITY` (default 0.05),
`OGSIM_AS_REORDER_PROBABILITY` (default 0.1) and `OGSIM_MARKET_SEED`.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ogsim.protocols.ercot_as_dispatch import AsDispatchBook, AsInstruction, DeliveryProfile
from ogsim.protocols.ercot_mms import MmsSimState

DEFAULT_AWARDS = "OG_ESR_1:ECRS:0.5,OG_ESR_1:RRS:0.5"
DEFAULT_RESOURCES = "OG_ESR_1,OG_ESR_2,OG_GEN_1"
UNKNOWN_RESOURCE = "OG_ESR_UNKNOWN"
#: Resource ids the scenario files may target (the sim's default QSE resources, plus one deliberately
#: holding no award for the unknown-award scenario).
SIMULATED_ERCOT_RESOURCES = frozenset({*DEFAULT_RESOURCES.split(","), UNKNOWN_RESOURCE})
DEFAULT_SERVICE = "ECRS"
DEFAULT_DURATION_MIN = 30
DEFAULT_RAMP_MIN = 10  # ECRS: full deployment within 10 minutes
DEFAULT_EXCEED_FACTOR = 3.0
OUT_OF_ORDER_DELAY_S = 20.0
AS_DISPATCH_TYPES = frozenset(
    {
        "ercot_as_deploy",
        "ercot_as_recall",
        "ercot_as_duplicate",
        "ercot_as_out_of_order",
        "ercot_as_malformed",
        "ercot_as_unknown_award",
        "ercot_as_exceed_award",
    }
)


def _pairs(raw: str) -> dict[tuple[str, str], float]:
    awards: dict[tuple[str, str], float] = {}
    for item in raw.split(","):
        parts = [p.strip() for p in item.split(":")]
        if len(parts) == 3 and parts[0] and parts[1]:
            awards[(parts[0], parts[1].upper())] = float(parts[2])
    return awards


def delivery_profile_from_env(seed: int) -> DeliveryProfile:
    low, _, high = os.environ.get("OGSIM_AS_LATENCY_S", "1,4").partition(",")
    return DeliveryProfile(
        latency_s=(float(low), float(high or low)),
        duplicate_probability=float(os.environ.get("OGSIM_AS_DUPLICATE_PROBABILITY", "0.05")),
        reorder_probability=float(os.environ.get("OGSIM_AS_REORDER_PROBABILITY", "0.1")),
        seed=seed,
    )


def mms_state_from_env(seed: int, clock: Callable[[], datetime]) -> MmsSimState:
    resources = {r.strip() for r in os.environ.get("OGSIM_MMS_RESOURCES", DEFAULT_RESOURCES).split(",")}
    return MmsSimState(
        qse_code=os.environ.get("OGSIM_MMS_QSE_CODE", "QOPENGRID"),
        resources={r for r in resources if r},
        delivery=delivery_profile_from_env(seed),
        clock=clock,
    )


@dataclass
class AsDispatchScenarios:
    """Turns one injected market anomaly into the dispatch instruction(s) ERCOT would send."""

    book: AsDispatchBook
    awards: dict[tuple[str, str], float]

    @classmethod
    def from_env(cls, book: AsDispatchBook) -> AsDispatchScenarios:
        return cls(book=book, awards=_pairs(os.environ.get("OGSIM_AS_AWARDS", DEFAULT_AWARDS)))

    def apply(self, anomaly_type: str, target: str, params: dict[str, Any], now: datetime) -> list[str]:
        """Publish the instruction(s) for this anomaly; returns their mRIDs."""
        resource = target
        if resource in ("", "*"):
            unknown = anomaly_type == "ercot_as_unknown_award"
            resource = UNKNOWN_RESOURCE if unknown else self._default_resource()
        service = str(params.get("service", DEFAULT_SERVICE)).upper()
        if anomaly_type == "ercot_as_recall":
            return self._recall(resource, service, now)
        mw = self._mw(anomaly_type, resource, service, params)
        malformed = (
            {"mw": "fifty", "startTime": "not-a-time"} if anomaly_type == "ercot_as_malformed" else None
        )
        deploy = self._deploy(resource, service, mw, params, now, malformed)
        if anomaly_type == "ercot_as_duplicate":
            self.book.force_duplicate(deploy.mrid)
        if anomaly_type == "ercot_as_out_of_order":
            deploy.visible_at = now + timedelta(seconds=OUT_OF_ORDER_DELAY_S)
            recall = self.book.publish(
                resource=resource,
                instruction_type="RECALL_AS",
                issued_at=now + timedelta(seconds=1),
                as_type=service,
                recall_of=deploy.mrid,
                text=f"Recall {service}",
            )
            recall.visible_at = now
            return [deploy.mrid, recall.mrid]
        return [deploy.mrid]

    def _default_resource(self) -> str:
        return next(iter(self.awards), ("OG_ESR_1", DEFAULT_SERVICE))[0]

    def _mw(self, anomaly_type: str, resource: str, service: str, params: dict[str, Any]) -> float:
        award_mw = self.awards.get((resource, service), 0.5)
        if anomaly_type == "ercot_as_exceed_award":
            return round(award_mw * float(params.get("factor", DEFAULT_EXCEED_FACTOR)), 3)
        requested = float(params.get("mw", 0.0) or 0.0)
        return requested if requested > 0 else award_mw

    def _deploy(
        self,
        resource: str,
        service: str,
        mw: float,
        params: dict[str, Any],
        now: datetime,
        malformed: dict[str, str] | None,
    ) -> AsInstruction:
        start = now + timedelta(seconds=float(params.get("start_in_s", 0.0)))
        duration = timedelta(minutes=float(params.get("duration_min", DEFAULT_DURATION_MIN)))
        return self.book.publish(
            resource=resource,
            instruction_type="DEPLOY_AS",
            issued_at=now,
            as_type=service,
            mw=mw,
            start=start,
            end=start + duration,
            ramp_minutes=int(params.get("ramp_min", DEFAULT_RAMP_MIN)),
            text=f"Deploy {mw:g} MW {service}",
            malformed_fields=malformed,
        )

    def _recall(self, resource: str, service: str, now: datetime) -> list[str]:
        latest = self.book.latest_deployment(resource, service)
        recall = self.book.publish(
            resource=resource,
            instruction_type="RECALL_AS",
            issued_at=now,
            as_type=service,
            recall_of=latest.mrid if latest is not None else None,
            text=f"Recall {service}",
        )
        return [recall.mrid]
