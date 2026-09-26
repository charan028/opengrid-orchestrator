"""`POST /og/api/alerts/ack-bulk` (operator, <=500 ids, per-id outcome through the single-ack path) and
`GET /og/api/alerts` (paging, filters, rule+scope grouping). The responses for the fixed alerts below are
the UI fixtures `fixtures/ui19/alerts_*.json` (MERGE)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from opengrid.api.deps import get_config, get_store
from opengrid.api.routers import health
from opengrid.core.models.platform import Alert
from opengrid.platform.config import Config

from .conftest import OPERATOR_HEADERS, PROXY_HEADERS, VIEWER_HEADERS, _echo_csrf_cookie_as_header
from .fakes import FakeStore

FIXTURES = Path(__file__).parent / "fixtures" / "ui19"
T0 = datetime(2026, 9, 26, 17, 0, tzinfo=UTC)
BULK = "/og/api/alerts/ack-bulk"
LIST = "/og/api/alerts"


def _alerts() -> list[Alert]:
    rows = [
        (1, "ALR-FEED-STALE", "warning", "FEED", "ERCOT:np6-905-cd", None, None),
        (2, "ALR-FEED-STALE", "warning", "FEED", "ERCOT:np6-905-cd", None, None),
        (3, "ALR-PROCESS-DOWN", "critical", "PROCESS", "engine", None, None),
        (4, "ALR-HUB-OFFLINE", "warning", "BANK", "bank-007", "operator", None),
        (5, "ALR-HUB-OFFLINE", "warning", "BANK", "bank-007", None, T0),  # cleared
    ]
    return [
        Alert(
            id=i,
            rule=rule,
            severity=severity,
            summary=f"{rule} {ref}",
            opened_at=T0 - timedelta(minutes=i),
            acked_by=acked_by,
            cleared_at=cleared_at,
            scope_kind=kind,
            scope_ref=ref,
        )
        for i, rule, severity, kind, ref, acked_by, cleared_at in rows
    ]


@pytest.fixture
def store() -> FakeStore:
    fake = FakeStore()
    fake.alerts = _alerts()
    return fake


@pytest.fixture
def api(store: FakeStore) -> TestClient:
    # A bare app with only this router (the contract does not depend on `create_app()`'s other mounts).
    app = FastAPI()
    app.include_router(health.router)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_config] = lambda: Config({"api": {"roles": {"operator": [], "viewer": []}}})
    client = TestClient(app, client=("127.0.0.1", 51234), headers=PROXY_HEADERS)
    client.event_hooks = {"request": [_echo_csrf_cookie_as_header], "response": []}
    return client


def _matches_fixture(body: dict[str, Any], name: str) -> bool:
    return body == json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --- bulk ack --------------------------------------------------------------------------------------------


def test_bulk_ack_reports_a_per_id_outcome(api: TestClient, store: FakeStore) -> None:
    resp = api.post(BULK, headers=OPERATOR_HEADERS, json={"alert_ids": [1, 3, 4, 99, 1]})

    assert resp.status_code == 200
    body = resp.json()
    assert body["results"] == [
        {"alert_id": 1, "outcome": "acked"},
        {"alert_id": 3, "outcome": "acked"},
        {"alert_id": 4, "outcome": "already_acked"},
        {"alert_id": 99, "outcome": "not_found"},
    ]
    assert body["counts"] == {"acked": 2, "already_acked": 1, "not_found": 1}
    by_id = {a.id: a for a in store.alerts}
    assert by_id[1].acked_by == "operator" and by_id[3].acked_by == "operator"
    assert by_id[4].acked_by == "operator"  # already acked: left as it was, never re-attributed
    assert _matches_fixture(body, "alerts_ack_bulk.json")


def test_bulk_ack_is_operator_only_and_bounded(api: TestClient, store: FakeStore) -> None:
    assert api.post(BULK, headers=VIEWER_HEADERS, json={"alert_ids": [1]}).status_code == 403
    assert api.post(BULK, headers=OPERATOR_HEADERS, json={"alert_ids": []}).status_code == 422
    assert api.post(BULK, headers=OPERATOR_HEADERS, json={"alert_ids": list(range(501))}).status_code == 422
    assert all(a.acked_by is None for a in store.alerts if a.id in (1, 2, 3))


def test_single_ack_still_works_through_the_shared_path(api: TestClient, store: FakeStore) -> None:
    resp = api.post(f"{LIST}/2/ack", headers=OPERATOR_HEADERS)
    assert resp.status_code == 200 and resp.json()["acked_by"] == "operator"
    assert api.post(f"{LIST}/99/ack", headers=OPERATOR_HEADERS).status_code == 404


# --- list: paging, filters, grouping ---------------------------------------------------------------------


def test_list_pages_open_alerts_newest_first(api: TestClient) -> None:
    body = api.get(LIST, headers=VIEWER_HEADERS, params={"limit": 2, "offset": 1}).json()
    assert body["total"] == 4  # alert 5 is cleared
    assert [a["id"] for a in body["alerts"]] == [2, 3]
    assert body["alerts"][0]["scope_kind"] == "FEED"
    assert _matches_fixture(body, "alerts_page.json")


def test_list_filters_by_severity_rule_and_scope(api: TestClient) -> None:
    critical = api.get(LIST, headers=VIEWER_HEADERS, params={"severity": "critical"}).json()
    assert [a["id"] for a in critical["alerts"]] == [3]
    by_scope = api.get(
        LIST,
        headers=VIEWER_HEADERS,
        params={"scope_kind": "BANK", "scope_ref": "bank-007", "open_only": False},
    ).json()
    assert [a["id"] for a in by_scope["alerts"]] == [4, 5]
    by_rule = api.get(LIST, headers=VIEWER_HEADERS, params={"rule": "ALR-FEED-STALE"}).json()
    assert by_rule["total"] == 2


def test_list_groups_by_rule_and_scope(api: TestClient) -> None:
    body = api.get(LIST, headers=VIEWER_HEADERS, params={"group": True}).json()
    assert body["groups"] == [
        {"rule": "ALR-FEED-STALE", "scope_kind": "FEED", "scope_ref": "ERCOT:np6-905-cd", "count": 2},
        {"rule": "ALR-HUB-OFFLINE", "scope_kind": "BANK", "scope_ref": "bank-007", "count": 1},
        {"rule": "ALR-PROCESS-DOWN", "scope_kind": "PROCESS", "scope_ref": "engine", "count": 1},
    ]
    assert _matches_fixture(body, "alerts_grouped.json")


def test_list_rejects_bad_paging(api: TestClient) -> None:
    assert api.get(LIST, headers=VIEWER_HEADERS, params={"limit": 501}).status_code == 422
    assert api.get(LIST, headers=VIEWER_HEADERS, params={"offset": -1}).status_code == 422
    assert api.get(LIST, headers=VIEWER_HEADERS, params={"severity": "info"}).status_code == 422
