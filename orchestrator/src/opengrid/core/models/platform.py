"""Row shapes for platform-owned tables (fleet twin, health) -- 02b S4, S6.4."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class _Row(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Hub(_Row):
    hub_id: str
    bank_id: str
    zone: str
    e_kwh: float
    r_kwh: float
    p_kw: float
    eta_c: float = 0.9487
    eta_d: float = 0.9487
    lat: float | None = None
    lon: float | None = None


class Bank(_Row):
    bank_id: str
    zone: str
    kva_rating: float
    reserve_kva: float = 0.0
    feeder_id: str | None = None


class HubState(_Row):
    hub_id: str
    soc_kwh: float
    p_kw: float
    health: Literal["online", "stale", "fault"] = "online"
    lease_epoch: int = 0
    lease_expires_at: datetime | None = None
    last_command_id: UUID | None = None
    last_seen_at: datetime
    fault_code: str | None = None


class Heartbeat(_Row):
    process: str
    pid: int
    ts: datetime
    status: str = "ok"


class Alert(_Row):
    id: int | None = None
    rule: str
    severity: Literal["warning", "critical"]
    summary: str
    detail: dict[str, Any] | None = None
    opened_at: datetime
    cleared_at: datetime | None = None
    acked_by: str | None = None


class FeedObs(_Row):
    source: str
    product: str
    series: str
    ts: datetime
    value: float
    unit: str
    quality: Literal["GOOD", "ESTIMATED", "STALE"]
    recorded_at: datetime


class FeedStatus(_Row):
    source: str
    product: str
    last_value_at: datetime | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    breaker_open: bool = False
    active_key: str | None = None
