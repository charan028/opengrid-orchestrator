"""Integration tests against `ogsim.market` (BUILD.md S4 "feeds" scope, TS-02-*).

Run on the server only, per BUILD.md S5:
    powershell -File tools\\remote.ps1 -Ws feeds -Cmd "cd orchestrator && python -m pytest tests/integration/feeds -q"
with `ogsim.market` started first in the background on a non-default port so it does not collide with
other workspaces, e.g.:
    PYTHONPATH=integration-sims/src OGSIM_MARKET_PORT=18090 /opt/ogsim/venv/bin/python -m ogsim.market &

Every test is skipped locally (no server Postgres, and `ogsim.market` is not running) via
`MARKET_SIM_URL`: set it to the sim's base URL to opt in (default probe: `http://127.0.0.1:18090`).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest

from opengrid.feeds.breaker import CONSECUTIVE_FAILURE_THRESHOLD
from opengrid.feeds.ercot import ErcotClient
from opengrid.feeds.http_client import FeedHttpError

MARKET_SIM_URL = os.environ.get("MARKET_SIM_URL", "http://127.0.0.1:18090")

# Matches ogsim.market's config.py defaults (`OGSIM_MARKET_TEST_USERS`/`_KEY_PRIMARY`/`_KEY_SECONDARY`).
SIM_USER = "test@example.com"
SIM_PASSWORD = "test"  # noqa: S105 - the market simulator's published test credential, not a real secret
SIM_KEY_PRIMARY = "test-primary-key"
SIM_KEY_SECONDARY = "test-secondary-key"


async def _market_reachable(base_url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(f"{base_url}/admin/healthz")
        return response.status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture
async def market_available() -> None:
    if not await _market_reachable(MARKET_SIM_URL):
        pytest.skip(f"ogsim.market not reachable at {MARKET_SIM_URL} (set MARKET_SIM_URL to opt in)")


@pytest.fixture
async def admin_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=MARKET_SIM_URL, timeout=5.0) as client:
        yield client


async def _inject(
    admin_client: httpx.AsyncClient,
    *,
    type_: str,
    target: str,
    duration: float = 30.0,
    params: dict[str, object] | None = None,
) -> str:
    anomaly_id = f"test-{uuid.uuid4().hex[:8]}"
    response = await admin_client.post(
        "/admin/anomalies",
        json={
            "id": anomaly_id,
            "type": type_,
            "target": target,
            "duration": duration,
            "params": params or {},
        },
    )
    response.raise_for_status()
    return anomaly_id


async def _cancel(admin_client: httpx.AsyncClient, anomaly_id: str) -> None:
    await admin_client.delete(f"/admin/anomalies/{anomaly_id}")


@pytest.fixture
def env_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_ERCOT_USER", SIM_USER)
    monkeypatch.setenv("TEST_ERCOT_PASSWORD", SIM_PASSWORD)
    monkeypatch.setenv("TEST_ERCOT_KEY_PRIMARY", SIM_KEY_PRIMARY)
    monkeypatch.setenv("TEST_ERCOT_KEY_SECONDARY", SIM_KEY_SECONDARY)


def _make_ercot_client(http_client: httpx.AsyncClient) -> ErcotClient:
    return ErcotClient(
        # market-api.md S1: the simulator's ERCOT base is `<sim>/ercot`, not production's
        # `/api/public-reports` (matches `orchestrator/config/test.toml`'s `feeds.ercot.base_url`).
        base_url=f"{MARKET_SIM_URL}/ercot",
        username_env="TEST_ERCOT_USER",
        password_env="TEST_ERCOT_PASSWORD",
        primary_key_env="TEST_ERCOT_KEY_PRIMARY",
        secondary_key_env="TEST_ERCOT_KEY_SECONDARY",
        http_client=http_client,
        token_url=f"{MARKET_SIM_URL}/token",
    )


@pytest.mark.usefixtures("market_available", "env_setup")
async def test_baseline_fetch_succeeds() -> None:
    async with httpx.AsyncClient() as http_client:
        client = _make_ercot_client(http_client)
        obs, events = await client.fetch_product("np6-905-cd", now=datetime.now(UTC))
    assert obs
    assert events == []


@pytest.mark.usefixtures("market_available", "env_setup")
async def test_401_primary_triggers_key_rotation(admin_client: httpx.AsyncClient) -> None:
    """TS-02-* / 02b S2.6: a 401 on the primary key rotates to the secondary and the call still
    succeeds (`ogsim.market`'s `http_401_primary` anomaly rejects only the primary key)."""
    anomaly_id = await _inject(admin_client, type_="http_401_primary", target="np6-905-cd")
    try:
        async with httpx.AsyncClient() as http_client:
            client = _make_ercot_client(http_client)
            obs, events = await client.fetch_product("np6-905-cd", now=datetime.now(UTC))
        assert obs
        assert len(events) == 1
        assert events[0].to_key == "SECONDARY"
        assert client.active_key == "SECONDARY"
    finally:
        await _cancel(admin_client, anomaly_id)


@pytest.mark.usefixtures("market_available", "env_setup")
async def test_429_raises_feed_http_error(admin_client: httpx.AsyncClient) -> None:
    """TS-02-02 / 02b S2.6: a 429 is retried (capped) then surfaces as `FeedHttpError`, never silently
    swallowed -- the caller (scheduler) is the one that turns repeated failures into a breaker trip."""
    # A short `retry_after_s` (well under the anomaly's own duration) keeps this test fast without
    # racing the anomaly's expiry: the client's Retry-After sleep must land safely inside the window.
    anomaly_id = await _inject(
        admin_client, type_="http_429", target="np6-345-cd", duration=30.0, params={"retry_after_s": 1}
    )
    try:
        async with httpx.AsyncClient() as http_client:
            client = _make_ercot_client(http_client)
            with pytest.raises(FeedHttpError) as exc_info:
                await client.fetch_product("np6-345-cd", now=datetime.now(UTC))
        assert exc_info.value.status_code == 429
    finally:
        await _cancel(admin_client, anomaly_id)


@pytest.mark.usefixtures("market_available", "env_setup")
async def test_5xx_opens_breaker_after_threshold(admin_client: httpx.AsyncClient) -> None:
    """02b S2.6: 5 consecutive failures open the circuit breaker, driven through the real
    `FeedsScheduler` against the live (anomaly-injected) simulator."""
    from opengrid.feeds.eia import EiaClient
    from opengrid.feeds.nws import NwsClient
    from opengrid.feeds.scheduler import FeedsScheduler
    from opengrid.feeds.token_bucket import TokenBucket

    anomaly_id = await _inject(admin_client, type_="http_5xx", target="np4-732-cd", duration=60.0)
    try:
        async with httpx.AsyncClient() as http_client:
            ercot = _make_ercot_client(http_client)
            eia = EiaClient(
                base_url=f"{MARKET_SIM_URL}/eia",
                api_key_env="TEST_EIA_KEY",
                respondent="ERCO",
                http_client=http_client,
            )
            nws = NwsClient(
                base_url=f"{MARKET_SIM_URL}/nws",
                user_agent="OpenGrid-Test (ops@example.com)",
                http_client=http_client,
            )
            scheduler = FeedsScheduler(
                ercot=ercot,
                eia=eia,
                nws=nws,
                store=_NullStore(),
                staleness_cfg={},
                nws_grid_point_pinned="EWX/156,91",
                ercot_bucket=TokenBucket(capacity=100, refill_per_s=100),
                trace=None,
            )
            now = datetime.now(UTC)
            for _ in range(CONSECUTIVE_FAILURE_THRESHOLD):
                await scheduler._poll_ercot_product("np4-732-cd", now=now)
            assert scheduler._ercot_breaker.is_open
    finally:
        await _cancel(admin_client, anomaly_id)


@pytest.mark.usefixtures("market_available", "env_setup")
async def test_stale_posting_freezes_the_timestamp(admin_client: httpx.AsyncClient) -> None:
    """02b S2.6 / TS-02-05: `ogsim.market`'s `stale_posting` anomaly freezes the simulated clock for
    one product -- `feeds` must see the *same* `interval_start`/`ts` on repeated polls (not a rolling
    "now"), which is what eventually pushes `age_s` past the staleness threshold and flips
    `effective_quality` to STALE (unit-tested in `test_staleness.py`; this proves the wire behavior the
    unit test assumes)."""
    anomaly_id = await _inject(admin_client, type_="stale_posting", target="np6-905-cd", duration=30.0)
    try:
        async with httpx.AsyncClient() as http_client:
            client = _make_ercot_client(http_client)
            obs_1, _ = await client.fetch_product("np6-905-cd", now=datetime.now(UTC))
            obs_2, _ = await client.fetch_product("np6-905-cd", now=datetime.now(UTC))
        assert obs_1 and obs_2
        assert {o.ts for o in obs_1} == {o.ts for o in obs_2}
    finally:
        await _cancel(admin_client, anomaly_id)


class _NullStore:
    """Minimal `FeedStore`-shaped no-op sink for the breaker test above -- it only needs the breaker
    behavior, not persistence."""

    async def upsert_obs(self, rows: list[object]) -> None:
        return None

    async def update_status(self, **kwargs: object) -> None:
        return None
