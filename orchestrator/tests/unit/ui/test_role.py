"""Tests for `opengrid.ui.role` (security fix, live-path finding 2026-09-26): identity comes ONLY from
the trusted `X-Remote-User` header (the one Apache itself sets from `REMOTE_USER`, per
`/etc/apache2/conf-enabled/opengrid.conf`), mapped through `[api.roles]` config
(`opengrid.api.auth.role_for_identity`) -- never from the client-controllable `X-OG-Role` header, which
Apache's `/og/` fragment never touches at all."""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from starlette.datastructures import Headers
from starlette.requests import Request

from opengrid.platform.config import Config
from opengrid.ui.role import is_operator, remote_user, role_of

_ROLES_CFG = Config({"api": {"roles": {"operator": ["alice"], "viewer": ["carol"]}}})


def _request(headers: dict[str, str] | None = None, *, config: Config | None = _ROLES_CFG) -> Request:
    app = FastAPI()
    if config is not None:
        app.state.config = config
    headers_obj = Headers(headers or {})
    scope = {"type": "http", "headers": headers_obj.raw, "method": "GET", "path": "/", "app": app}
    return Request(scope)


def test_role_of_defaults_to_viewer_with_no_identity() -> None:
    assert role_of(_request()) == "viewer"
    assert is_operator(_request()) is False


def test_role_of_ignores_a_client_supplied_x_og_role_header() -> None:
    """The header a client can set directly must never grant a role by itself -- Apache's `/og/`
    fragment only ever manages `X-Remote-User`, so `X-OG-Role: operator` alone (no valid identity) must
    stay a viewer, not escalate."""
    request = _request({"X-OG-Role": "operator"})
    assert role_of(request) == "viewer"
    assert is_operator(request) is False


def test_role_of_ignores_x_og_role_even_alongside_a_viewer_identity() -> None:
    """A real (but low-privileged) identity plus a spoofed role header must resolve from the identity,
    not the spoofed header -- otherwise a viewer account could self-escalate."""
    request = _request({"X-OG-Role": "operator", "X-Remote-User": "carol"})
    assert role_of(request) == "viewer"
    assert is_operator(request) is False


def test_role_of_honors_the_configured_operator_identity() -> None:
    request = _request({"X-Remote-User": "alice"})
    assert role_of(request) == "operator"
    assert is_operator(request) is True


def test_role_of_falls_back_to_the_literal_apache_account_names_with_no_config() -> None:
    request = _request({"X-Remote-User": "operator"}, config=Config({}))
    assert role_of(request) == "operator"


def test_role_of_denies_an_unmapped_identity(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="opengrid.ui.role"):
        request = _request({"X-Remote-User": "mallory"})
        role = role_of(request)

    assert role == "viewer"
    assert is_operator(request) is False
    assert any("mallory" in r.getMessage() for r in caplog.records)


def test_role_of_denies_when_app_state_has_no_config(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="opengrid.ui.role"):
        request = _request({"X-Remote-User": "alice"}, config=None)
        role = role_of(request)

    assert role == "viewer"
    assert len(caplog.records) == 1


def test_remote_user_reads_only_the_trusted_header() -> None:
    assert remote_user(_request({"X-Remote-User": "alice"})) == "alice"
    assert remote_user(_request({"X-OG-Role": "operator"})) is None
    assert remote_user(_request()) is None
