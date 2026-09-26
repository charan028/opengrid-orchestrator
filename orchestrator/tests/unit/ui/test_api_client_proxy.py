"""`opengrid.ui.api_client` forwards the proxy secret with the identity, so the API believes the
proxy-verified user the UI relays (`opengrid.api.auth.proxy_authenticated`)."""

from __future__ import annotations

import pytest

from opengrid.api.auth import PROXY_SECRET_ENV
from opengrid.ui.api_client import _identity_headers


def test_identity_headers_carry_the_proxy_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROXY_SECRET_ENV, "s3")
    assert _identity_headers("alice") == {"X-Remote-User": "alice", "X-OG-Proxy-Auth": "s3"}


def test_no_identity_sends_no_identity_headers() -> None:
    assert _identity_headers(None) is None
