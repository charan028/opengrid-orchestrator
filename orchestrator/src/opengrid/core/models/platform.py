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
    units: Literal[1, 2] = 1
    """Battery units in the home (migration 0032's `og.hub.units`): 2 for a dual-unit home, else 1. The
    guardian's G-02 cap is 11 kW per unit and 20 kW for a dual-unit home, so the seed writes it explicitly."""


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
    # Full hub-health vocabulary used across fleet/health/api; "offline" is persisted by the health evaluator.
    health: Literal["online", "stale", "offline", "fault"] = "online"
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
