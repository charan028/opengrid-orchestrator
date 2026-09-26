"""Review #16: og-api trusts identity headers only from the local Apache proxy, so it refuses a
non-loopback bind unless explicitly overridden."""

from __future__ import annotations

import pytest

from opengrid.api.main import resolve_bind_host
from opengrid.platform.config import Config


def test_loopback_binds_are_accepted() -> None:
    assert resolve_bind_host(Config({})) == "127.0.0.1"
    assert resolve_bind_host(Config({"api": {"bind_host": "::1"}})) == "::1"


def test_a_non_loopback_bind_is_refused() -> None:
    with pytest.raises(SystemExit):
        resolve_bind_host(Config({"api": {"bind_host": "0.0.0.0"}}))  # noqa: S104 -- the refused case


def test_an_explicit_override_allows_it() -> None:
    cfg = Config({"api": {"bind_host": "10.0.0.5", "allow_non_loopback_bind": True}})
    assert resolve_bind_host(cfg) == "10.0.0.5"
