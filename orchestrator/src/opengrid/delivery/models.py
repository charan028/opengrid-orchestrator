"""Delivery-verification data crossing process boundaries (D-38): the per-call record (`og.delivery_record`,
migration 0050), its per-bucket series and the live point the grid link publishes. Sign convention
+charge/-discharge on every kW field; energy fields are discharged kWh (>= 0)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from opengrid.core.delivery import SIGN_CONVENTION, DeliveryBucket


class CallKind(StrEnum):
    UTILITY_CALL = "UTILITY_CALL"
    AS_DEPLOYMENT = "AS_DEPLOYMENT"
    MANUAL_TARGET = "MANUAL_TARGET"


@dataclass(frozen=True, slots=True)
class CallSpec:
    """A discharge call to verify: what was committed, when, and on which banks/hubs."""

    call_id: str
    call_kind: CallKind
    window_start: datetime
    window_end: datetime
    committed_kw: float
    product: str | None = None
    deployment_id: UUID | None = None
    dispatch_call_id: UUID | None = None
    obligation_id: UUID | None = None
    contract_id: UUID | None = None
    customer_id: UUID | None = None
    utility_id: str | None = None
    service_type: str | None = None
    stopped_at: datetime | None = None
    idempotency_key: str | None = None
    bank_ids: tuple[str, ...] = ()
    meter_bank_ids: tuple[str, ...] = ()
    hub_ids: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def effective_end(self) -> datetime:
        return min(self.window_end, self.stopped_at) if self.stopped_at is not None else self.window_end


class SeriesPoint(BaseModel):
    """One bucket: `t` start, `s` seconds, `c` committed, `m` commanded (signed batches), `d` delivered
    (None = no telemetry), `p`/`v` proposed/vetoed command cycles, `md`/`bd` meter/battery change from
    their pre-call baselines on the metered banks."""

    model_config = ConfigDict(frozen=True)

    t: datetime
    s: float
    c: float
    m: float | None = None
    d: float | None = None
    p: int = 0
    v: int = 0
    md: float | None = None
    bd: float | None = None

    @classmethod
    def of(cls, bucket: DeliveryBucket) -> SeriesPoint:
        return cls(
            t=bucket.start,
            s=bucket.seconds,
            c=bucket.committed_kw,
            m=bucket.commanded_kw,
            d=bucket.delivered_kw,
            p=bucket.proposed_cycles,
            v=bucket.vetoed_cycles,
            md=bucket.meter_delta_kw,
            bd=bucket.battery_delta_kw,
        )

    def bucket(self) -> DeliveryBucket:
        return DeliveryBucket(
            start=self.t,
            end=self.t + timedelta(seconds=self.s),
            committed_kw=self.c,
            commanded_kw=self.m,
            delivered_kw=self.d,
            proposed_cycles=self.p,
            vetoed_cycles=self.v,
            meter_delta_kw=self.md,
            battery_delta_kw=self.bd,
        )


class DeliveryRecord(BaseModel):
    """`og.delivery_record`: the verification of one call (live while `final` is false)."""

    model_config = ConfigDict(frozen=True)

    call_id: str
    call_kind: CallKind
    deployment_id: UUID | None = None
    dispatch_call_id: UUID | None = None
    obligation_id: UUID | None = None
    contract_id: UUID | None = None
    customer_id: UUID | None = None
    utility_id: str | None = None
    service_type: str | None = None
    product: str | None = None
    bank_ids: list[str] = []
    meter_bank_ids: list[str] = []
    hub_ids: list[str] = []
    window_start: datetime
    window_end: datetime
    stopped_at: datetime | None = None
    committed_kw: float
    commanded_kw_avg: float | None = None
    delivered_kw_avg: float | None = None
    delivered_kw_last: float | None = None
    commanded_kw_last: float | None = None
    ramp_time_s: float
    time_to_target_s: float | None = None
    sustained_pct: float | None = None
    lowest_kw: float | None = None
    lowest_at: datetime | None = None
    lowest_run_s: float | None = None
    discharged_kwh: float = 0.0
    committed_kwh: float = 0.0
    stale_frac: float = 0.0
    result: str
    reasons: list[str] = []
    meter_status: str = "NO_METER"
    meter_mismatch_frac: float | None = None
    meter_baseline_kw: float | None = None
    battery_baseline_kw: float | None = None
    series: list[SeriesPoint] = []
    evaluated_to: datetime | None = None
    final: bool = False
    trace_id: UUID | None = None
    #: When retention (migration 0051) emptied `series`; None while the per-bucket series is kept.
    series_pruned_at: datetime | None = None
    updated_at: datetime | None = None

    def public(self, *, with_series: bool = False) -> dict[str, Any]:
        """JSON form for API responses; the series only on request (a detail view)."""
        body = self.model_dump(mode="json", exclude=None if with_series else {"series"})
        body["sign_convention"] = SIGN_CONVENTION
        return body


class LiveDeliveryPoint(BaseModel):
    """What the grid link publishes per active or just-ended call (read from `og.delivery_record`).
    `stale`: the latest bucket had no telemetry (or nothing evaluated yet) -- map to a comms-lost flag."""

    model_config = ConfigDict(frozen=True)

    call_id: str
    call_kind: CallKind
    deployment_id: UUID | None
    dispatch_call_id: UUID | None
    idempotency_key: str | None
    obligation_id: UUID | None
    contract_id: UUID | None
    utility_id: str | None
    bank_ids: list[str]
    committed_kw: float
    commanded_kw: float | None
    delivered_kw: float | None
    discharged_kwh: float
    state: str  # ACTIVE | ENDED
    result: str
    meter_status: str
    stale: bool
    as_of: datetime | None
