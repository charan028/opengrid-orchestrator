"""Talks to ogsim.market's internal admin API to inject/cancel/list market
anomalies. Base URL configurable via OGSIM_MARKET_ADMIN_URL (default
http://127.0.0.1:8090)."""

from __future__ import annotations

import os
from typing import Any

import httpx


def market_base_url() -> str:
    return os.environ.get("OGSIM_MARKET_ADMIN_URL", "http://127.0.0.1:8090")


async def inject(anomaly: dict[str, Any], base_url: str | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=base_url or market_base_url(), timeout=10.0) as client:
        resp = await client.post("/admin/anomalies", json=anomaly)
        resp.raise_for_status()
        return resp.json()


async def cancel(anomaly_id: str, base_url: str | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=base_url or market_base_url(), timeout=10.0) as client:
        resp = await client.delete(f"/admin/anomalies/{anomaly_id}")
        resp.raise_for_status()
        return resp.json()


async def list_active(base_url: str | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=base_url or market_base_url(), timeout=10.0) as client:
        resp = await client.get("/admin/anomalies")
        resp.raise_for_status()
        return resp.json()
