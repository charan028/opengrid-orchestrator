"""One LIVE ERCOT smoke test (BUILD.md S4 "feeds" scope): read-only, a single call, skipped if the
real ERCOT credentials are absent. Never runs as part of `pytest tests/unit` or the sim-backed
integration suite -- only when explicitly invoked with real credentials present in the environment
(`ERCOT_API_USER`, `ERCOT_API_PASSWORD`, `ERCOT_PUBLIC_API_KEY_PRIMARY`), e.g. on `basepower` where
`/etc/opengrid/api_keys.env` is loaded.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import httpx
import pytest

from opengrid.feeds.ercot import ErcotClient

_REQUIRED_ENV_VARS = ("ERCOT_API_USER", "ERCOT_API_PASSWORD", "ERCOT_PUBLIC_API_KEY_PRIMARY")

pytestmark = pytest.mark.skipif(
    any(not os.environ.get(v) for v in _REQUIRED_ENV_VARS),
    reason="live ERCOT smoke test requires real credentials in the environment",
)


async def test_live_ercot_spp_single_call() -> None:
    """A single, read-only GET against the real ERCOT Public API: settlement point prices. Proves the
    ROPC auth flow (`response_type=id_token`, reading the `id_token` field) and the NP6-905-CD
    normalization work end-to-end against production, without any write side effect (feeds normally
    writes to `feed_obs`; this test only calls the client, never the store).

    Run with `--tb=no` (BUILD.md S5a/S6: a live credential rejection must never render a traceback with
    the request body in a local-variable frame). `ErcotClient` also masks the password/keys in any
    exception message itself (`opengrid.feeds.secrets.Secret`) and this test re-raises without the
    original traceback as a second, code-level backstop, so the assertion below is meaningful even if
    `--tb=no` is left off the invocation.
    """
    try:
        async with httpx.AsyncClient() as http_client:
            client = ErcotClient(
                base_url="https://api.ercot.com/api/public-reports",
                username_env="ERCOT_API_USER",
                password_env="ERCOT_API_PASSWORD",
                primary_key_env="ERCOT_PUBLIC_API_KEY_PRIMARY",
                secondary_key_env="ERCOT_PUBLIC_API_KEY_SECONDARY",
                http_client=http_client,
            )
            obs, _events = await client.fetch_product("np6-905-cd", now=datetime.now(UTC))
    except Exception as exc:
        # Deliberately broad: re-raise stripped of local frames (BUILD.md S5a/S6).
        raise AssertionError(f"live ERCOT call failed: {exc}") from None

    assert obs
    for row in obs:
        assert row.source == "ERCOT"
        assert row.unit == "usd_per_mwh"
