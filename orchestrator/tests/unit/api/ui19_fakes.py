"""In-memory `ExtViewsProtocol` and app wiring for the Gitea #19 endpoint tests (no Postgres)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from opengrid.api.app import create_app
from opengrid.api.deps import get_config, get_proposals, get_store, get_trace_store
from opengrid.api.routers import (
    dispatch_ledger,
    fleet_bulk,
    fleet_map,
    grid_layers,
    markets_funnel,
    profitability_kw,
)
from opengrid.api.views_ext import (
    CommitmentSlice,
    ContractMarketRow,
    FunnelRow,
    MmsFunnelRow,
    ReservationSlice,
    get_ext_views,
)
from opengrid.market.economics import PeriodTotals

from .fakes import FakeStore

ZONES = ("LZ_NORTH", "LZ_SOUTH", "LZ_HOUSTON", "LZ_WEST")
COMMITTED_OBLIGATION = UUID("00000000-0000-7000-8000-00000000a001")
SELECTED_OBLIGATION = UUID("00000000-0000-7000-8000-00000000a002")
NOW = datetime.now(UTC)

UI19_ROUTERS = (fleet_map, grid_layers, fleet_bulk, dispatch_ledger, markets_funnel, profitability_kw)


def hub_row(
    index: int,
    *,
    banks: int = 40,
    kw: float | None = 0.0,
    health: str | None = "online",
    bank_has_grant: bool = False,
    lat: float | None = None,
    lon: float | None = None,
    obligations: list[dict[str, Any]] | None = None,
    soc_kwh: float = 20.0,
) -> dict[str, Any]:
    bank = index % banks
    return {
        "hub_id": f"hub-{index:05d}",
        "bank_id": f"bank-{bank:03d}",
        "zone": ZONES[bank % len(ZONES)],
        "lat": lat,
        "lon": lon,
        "e_kwh": 39.2,
        "r_kwh": 7.84,
        "rated_kw": 11.0,
        "soc_kwh": soc_kwh,
        "kw": kw,
        "health": health,
        "last_seen_at": NOW,
        "fault_code": None,
        "home_load_kw": None,
        "meter_kw": None,
        "pv_kw": None,
        "bank_has_grant": bank_has_grant,
        "bank_granted_kw": 55.0 if bank_has_grant else 0.0,
        "serving_obligations": obligations or [],
    }


def fleet_rows(n: int = 2000) -> list[dict[str, Any]]:
    """A varied fleet: every activity is represented, bank-000 delivers a committed obligation."""
    rows = []
    for i in range(n):
        bank = i % 40
        if bank == 0:
            row = hub_row(
                i,
                kw=5.0,
                bank_has_grant=True,
                obligations=[
                    {
                        "obligation_id": str(COMMITTED_OBLIGATION),
                        "service_type": "ERCOT_ENERGY",
                        "state": "DELIVERING",
                        "tier": "T2",
                    }
                ],
            )
        elif bank == 1:
            row = hub_row(i, kw=-4.0)
        elif bank == 2:
            row = hub_row(i, kw=3.0)
        elif bank == 3 and i < 200:
            row = hub_row(i, health="fault", kw=0.0)
        elif bank == 4:
            row = hub_row(i, health=None, kw=None)
        else:
            row = hub_row(i, kw=0.1)
        rows.append(row)
    return rows


@dataclass
class FakeExtViews:
    rows: list[dict[str, Any]] = field(default_factory=fleet_rows)
    contract_markets: list[ContractMarketRow] = field(
        default_factory=lambda: [
            ContractMarketRow("ERCOT_ENERGY", "FREE", None),
            ContractMarketRow("REGULATED_CAPACITY", "REGULATED", "AUSTIN_ENERGY"),
        ]
    )
    critical: set[tuple[str, str]] = field(default_factory=set)
    site_readings: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {
                "customer_id": "00000000-0000-7000-8000-0000000000c6",
                "site_id": "site-dc-01",
                "ts": NOW,
                "p_kw": 2400.0,
                "quality": "GOOD",
            },
            {"customer_id": "cust-x", "site_id": "site-unknown", "ts": NOW, "p_kw": -10.0, "quality": "GOOD"},
        ]
    )
    services: dict[str, list[str]] = field(
        default_factory=lambda: {"00000000-0000-7000-8000-0000000000c6": ["DATA_CENTER"]}
    )
    weather_live: dict[str, tuple[float, datetime]] = field(
        default_factory=lambda: {"coast": (18000.0, NOW), "north": (1500.0, NOW), "northC": (19000.0, NOW)}
    )
    bank_scada: dict[str, tuple[float, datetime]] = field(default_factory=lambda: {"bank-000": (400.0, NOW)})
    reservations: list[ReservationSlice] = field(default_factory=list)
    commitments: list[CommitmentSlice] = field(default_factory=list)
    funnel: list[FunnelRow] = field(default_factory=list)
    mms: list[MmsFunnelRow] | None = None
    totals: list[PeriodTotals] = field(default_factory=list)
    calls: dict[str, int] = field(default_factory=dict)

    def _count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    async def fleet_map_rows(self, *, grant_fresh_s: float) -> list[dict[str, Any]]:
        self._count("fleet_map_rows")
        return self.rows

    async def active_contract_markets(self) -> list[ContractMarketRow]:
        self._count("active_contract_markets")
        return self.contract_markets

    async def critical_alert_scopes(self) -> set[tuple[str, str]]:
        return self.critical

    async def customer_site_readings(self, *, max_age_s: float) -> list[dict[str, Any]]:
        return self.site_readings

    async def customer_contract_services(self) -> dict[str, list[str]]:
        return self.services

    async def weather_zone_load_mw(self) -> dict[str, tuple[float, datetime]]:
        return self.weather_live

    async def bank_scada_load_kw(self) -> dict[str, tuple[float, datetime]]:
        return self.bank_scada

    async def ledger_slices(
        self, *, t0: datetime, t1: datetime
    ) -> tuple[list[ReservationSlice], list[CommitmentSlice]]:
        return self.reservations, self.commitments

    async def funnel_rows(self, *, t0: datetime, t1: datetime, bucket: str) -> list[FunnelRow]:
        return self.funnel

    async def mms_funnel_rows(self, *, t0: datetime, t1: datetime, bucket: str) -> list[MmsFunnelRow] | None:
        return self.mms

    async def contract_totals(self, *, start: datetime, end: datetime) -> list[PeriodTotals]:
        self.calls["contract_totals_window"] = int((end - start) / timedelta(hours=1))
        return self.totals


class FleetFakeStore(FakeStore):
    """`FakeStore` that knows every hub of `fleet_rows()` (the single-hub command path looks each up)."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        super().__init__()
        self._rows = {r["hub_id"]: r for r in rows}

    async def get_hub(self, hub_id: str) -> dict[str, Any] | None:
        row = self._rows.get(hub_id)
        if row is None:
            return None
        return {**row, "p_kw": row["kw"], "lease_epoch": 1, "lease_expires_at": None, "last_command_id": None}


def build_app(
    views: FakeExtViews, store: FakeStore, trace_store: Any, proposals: Any, config: Any
) -> FastAPI:
    app = create_app()
    mounted = {getattr(r, "path", None) for r in app.routes}
    if "/og/api/fleet/map" not in mounted:  # until app.py carries the #19 mount lines
        for module in UI19_ROUTERS:
            app.include_router(module.router)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_trace_store] = lambda: trace_store
    app.dependency_overrides[get_proposals] = lambda: proposals
    app.dependency_overrides[get_config] = lambda: config
    app.dependency_overrides[get_ext_views] = lambda: views
    return app


def make_client(app: FastAPI, proxy_headers: dict[str, str]) -> TestClient:
    from .conftest import _echo_csrf_cookie_as_header

    client = TestClient(app, client=("127.0.0.1", 51234), headers=proxy_headers)
    client.event_hooks = {"request": [_echo_csrf_cookie_as_header], "response": []}
    return client
