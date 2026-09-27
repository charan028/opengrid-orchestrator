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
    # Battery/inverter units in the home (migration 0032): 1 = 11 kW, 2 = 20 kW dual-unit (G-02 unit cap).
    units: int = 1
    # Derived, not a column: the hub's bank is an og.asset SUBSTATION (0025; D-29 utility toll), so it is
    # rated at nameplate p_kw, never the home per-unit cap.
    utility_scale: bool = False


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
    # Latest optional discharge-flow telemetry (migration 0027); None until a hub reports it.
    home_load_kw: float | None = None
    pv_kw: float | None = None
    meter_kw: float | None = None
    cell_temp_c: float | None = None
    p_dis_max_kw: float | None = None
    p_ch_max_kw: float | None = None
    peak_power_budget_kws: float | None = None
    # D-28 charge-source split (migration 0034); None until a hub reports it.
    charge_pv_kw: float | None = None
    charge_grid_kw: float | None = None


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
    # Structured scope columns (migration 0024), additive alongside `detail`'s rule-specific fields --
    # so the UI/API can key off a real scope instead of parsing `summary` text.
    scope_kind: str | None = None
    scope_ref: str | None = None


class FeedObs(_Row):
    source: str
    product: str
    series: str
    ts: datetime
    value: float
    unit: str
    #: EXTREME_UNCORROBORATED (FR-ING-117 / V-P1, migration 0045): a real-time price outside the normal
    #: -$250..$5,000/MWh band but inside the hard bounds, not yet corroborated -- stored, never clipped.
    quality: Literal["GOOD", "ESTIMATED", "STALE", "EXTREME_UNCORROBORATED"]
    recorded_at: datetime


class FeedStatus(_Row):
    source: str
    product: str
    last_value_at: datetime | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    breaker_open: bool = False
    active_key: str | None = None
