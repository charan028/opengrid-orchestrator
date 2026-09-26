"""opengrid.api.routers.admin: operator-only fleet-topology reseed endpoint. Monkeypatches
`run_seed` itself -- its own behavior (id scheme, idempotent upsert) is covered by
`tests/unit/fleet/test_seed.py`; this only checks routing/auth/response shape."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from opengrid.api.deps import get_pool
from opengrid.api.routers import admin as admin_router

from .conftest import OPERATOR_HEADERS, VIEWER_HEADERS


@dataclass
class _FakeSeedResult:
    banks_upserted: int
    hubs_upserted: int


async def _fake_run_seed(cfg, pool):
    _ = cfg, pool
    return _FakeSeedResult(banks_upserted=40, hubs_upserted=2000)


def test_seed_fleet_topology_requires_operator(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(admin_router, "run_seed", _fake_run_seed)
    client.app.dependency_overrides[get_pool] = lambda: object()
    resp = client.post("/og/api/admin/seed-fleet-topology", headers=VIEWER_HEADERS)
    assert resp.status_code == 403


def test_seed_fleet_topology_ok_for_operator(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(admin_router, "run_seed", _fake_run_seed)
    client.app.dependency_overrides[get_pool] = lambda: object()
    resp = client.post("/og/api/admin/seed-fleet-topology", headers=OPERATOR_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"banks_upserted": 40, "hubs_upserted": 2000}
