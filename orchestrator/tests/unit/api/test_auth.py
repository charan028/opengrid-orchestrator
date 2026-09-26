"""Role enforcement (02b S7) and the `/og/api/health` loopback-only gate."""

from __future__ import annotations

from fastapi.testclient import TestClient

from opengrid.api.auth import PROXY_SECRET_ENV, Role, role_for_identity
from opengrid.platform.config import Config

from .conftest import OPERATOR_HEADERS, PROXY_SECRET, VIEWER_HEADERS


def test_role_for_identity_defaults_match_apache_account_names() -> None:
    cfg = Config({})
    assert role_for_identity("operator", cfg) is Role.OPERATOR
    assert role_for_identity("viewer", cfg) is Role.VIEWER
    assert role_for_identity("someone-else", cfg) is None


def test_role_for_identity_honors_configured_named_accounts() -> None:
    cfg = Config({"api": {"roles": {"operator": ["alice"], "viewer": ["bob"]}}})
    assert role_for_identity("alice", cfg) is Role.OPERATOR
    assert role_for_identity("bob", cfg) is Role.VIEWER
    assert role_for_identity("carol", cfg) is None


def test_missing_header_is_rejected(client) -> None:
    resp = client.get("/og/api/fleet/hubs")
    assert resp.status_code == 401


def test_unmapped_identity_is_forbidden(client) -> None:
    resp = client.get("/og/api/fleet/hubs", headers={"X-Remote-User": "nobody"})
    assert resp.status_code == 403


def test_viewer_can_read(client) -> None:
    resp = client.get("/og/api/fleet/hubs", headers=VIEWER_HEADERS)
    assert resp.status_code == 200


def test_viewer_cannot_write(client) -> None:
    resp = client.post(
        "/og/api/fleet/command",
        headers=VIEWER_HEADERS,
        json={"bank_id": "bank-01", "p_kw_setpoint": 1.0, "reason": "test"},
    )
    assert resp.status_code == 403


def test_operator_can_write(client) -> None:
    resp = client.post(
        "/og/api/fleet/command",
        headers=OPERATOR_HEADERS,
        json={"bank_id": "bank-01", "p_kw_setpoint": 1.0, "reason": "test"},
    )
    assert resp.status_code == 202


def test_health_accepts_loopback_client_without_any_header(client) -> None:
    resp = client.get("/og/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_health_rejects_non_loopback_client_even_with_header(non_loopback_client) -> None:
    resp = non_loopback_client.get("/og/api/health", headers=OPERATOR_HEADERS)
    assert resp.status_code == 403


def _unproxied(client: TestClient) -> TestClient:
    """A loopback caller that did not come through Apache: no `X-OG-Proxy-Auth` by default."""
    return TestClient(client.app, client=("127.0.0.1", 40000))


def test_identity_without_proxy_secret_is_rejected(client) -> None:
    """Any local process can reach the loopback port and set `X-Remote-User: operator` itself."""
    resp = _unproxied(client).get("/og/api/fleet/hubs", headers=OPERATOR_HEADERS)
    assert resp.status_code == 401


def test_identity_with_wrong_proxy_secret_is_rejected(client) -> None:
    resp = _unproxied(client).get(
        "/og/api/fleet/hubs", headers={**OPERATOR_HEADERS, "X-OG-Proxy-Auth": PROXY_SECRET + "x"}
    )
    assert resp.status_code == 401


def test_identity_is_rejected_when_no_proxy_secret_is_configured(client, monkeypatch) -> None:
    """Fail closed: an unset secret authenticates nothing, even a request presenting an empty header."""
    monkeypatch.delenv(PROXY_SECRET_ENV)
    assert client.get("/og/api/fleet/hubs", headers=OPERATOR_HEADERS).status_code == 401
    empty = _unproxied(client).get("/og/api/fleet/hubs", headers={**OPERATOR_HEADERS, "X-OG-Proxy-Auth": ""})
    assert empty.status_code == 401


def test_health_probe_stays_exempt_from_the_proxy_secret(client) -> None:
    assert _unproxied(client).get("/og/api/health").status_code == 200


def test_customer_role_is_only_an_explicitly_configured_account() -> None:
    cfg = Config({"api": {"roles": {"customer": {"acme": "00000000-0000-7000-8000-0000000000c6"}}}})
    assert role_for_identity("acme", cfg) is Role.CUSTOMER
    assert role_for_identity("customer", cfg) is None


def test_customer_cannot_read_operator_endpoints(client, fake_config) -> None:
    fake_config.as_dict()["api"]["roles"]["customer"] = {"acme": "00000000-0000-7000-8000-0000000000c6"}
    resp = client.get("/og/api/fleet/hubs", headers={"X-Remote-User": "acme"})
    assert resp.status_code == 403
