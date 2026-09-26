"""opengrid.api.csrf: double-submit-cookie CSRF mitigation (qa/security-review.md F-03)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from opengrid.api.csrf import COOKIE_NAME, HEADER_NAME, CSRFMiddleware


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CSRFMiddleware, allowed_hosts=frozenset({"good.example"}), cookie_secure=False)

    @app.get("/get")
    async def _get() -> dict[str, str]:
        return {"ok": "yes"}

    @app.post("/post")
    async def _post() -> dict[str, str]:
        return {"ok": "yes"}

    return app


def test_get_sets_csrf_cookie_when_absent() -> None:
    client = TestClient(_app())
    resp = client.get("/get")
    assert resp.status_code == 200
    assert COOKIE_NAME in resp.cookies


def test_post_with_no_cookie_and_no_origin_is_allowed() -> None:
    """Non-browser API/CLI callers (never fetched a GET, no Origin header) are not the CSRF threat."""
    client = TestClient(_app())
    resp = client.post("/post")
    assert resp.status_code == 200


def test_post_with_disallowed_origin_is_rejected_even_without_a_cookie() -> None:
    client = TestClient(_app())
    resp = client.post("/post", headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403


def test_post_with_allowed_origin_and_no_cookie_is_allowed() -> None:
    client = TestClient(_app())
    resp = client.post("/post", headers={"Origin": "https://good.example"})
    assert resp.status_code == 200


def test_post_with_cookie_but_no_token_header_is_rejected() -> None:
    client = TestClient(_app())
    client.get("/get")  # establishes the cookie
    resp = client.post("/post")
    assert resp.status_code == 403


def test_post_with_cookie_and_matching_header_is_allowed() -> None:
    client = TestClient(_app())
    client.get("/get")
    token = client.cookies.get(COOKIE_NAME)
    assert token is not None
    resp = client.post("/post", headers={HEADER_NAME: token})
    assert resp.status_code == 200


def test_post_with_cookie_and_wrong_token_header_is_rejected() -> None:
    client = TestClient(_app())
    client.get("/get")
    resp = client.post("/post", headers={HEADER_NAME: "wrong-token-value"})
    assert resp.status_code == 403


def test_post_with_cookie_and_matching_form_field_is_allowed() -> None:
    client = TestClient(_app())
    client.get("/get")
    token = client.cookies.get(COOKIE_NAME)
    assert token is not None
    resp = client.post("/post", data={"csrf_token": token})
    assert resp.status_code == 200
