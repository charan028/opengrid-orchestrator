"""Tests for ogsim.control.market_client against a stubbed httpx transport
(no real market server needed)."""

from __future__ import annotations

import pytest

from ogsim.control import market_client

# Captured at import time, before conftest's autouse fixture replaces
# `market_client.inject` with a stub for every test in the suite - these
# tests are exactly what exercises the real implementation.
_real_inject = market_client.inject


async def test_inject_posts_to_admin_anomalies(monkeypatch: pytest.MonkeyPatch):
    captured = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, path, json):
            captured["path"] = path
            captured["json"] = json

            class Resp:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"ok": True}

            return Resp()

    monkeypatch.setattr(market_client.httpx, "AsyncClient", FakeClient)
    result = await _real_inject({"id": "a1", "type": "price_spike"})
    assert result == {"ok": True}
    assert captured["path"] == "/admin/anomalies"
    assert captured["json"]["id"] == "a1"


def test_market_base_url_defaults_to_localhost(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OGSIM_MARKET_ADMIN_URL", raising=False)
    assert market_client.market_base_url() == "http://127.0.0.1:8090"


def test_market_base_url_honors_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OGSIM_MARKET_ADMIN_URL", "http://example.test:9999")
    assert market_client.market_base_url() == "http://example.test:9999"
