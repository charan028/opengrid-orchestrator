"""NWS (api.weather.gov) client (02b S2.4): grid-point resolution once at startup, then hourly forecast
polling with `If-Modified-Since`. No API key; a descriptive `User-Agent` is mandatory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from email.utils import format_datetime

import httpx

from opengrid.core.models.platform import FeedObs
from opengrid.feeds.http_client import FeedHttpError, request_with_retry
from opengrid.feeds.normalize import nws_forecast_to_feed_obs


@dataclass
class GridPoint:
    office: str
    x: int
    y: int


@dataclass
class NwsClient:
    base_url: str
    user_agent: str
    http_client: httpx.AsyncClient
    _grid_point: GridPoint | None = field(default=None, init=False, repr=False)
    _last_updated: datetime | None = field(default=None, init=False, repr=False)

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agent}

    async def resolve_grid_point(
        self, *, lat: float | None = None, lon: float | None = None, pinned: str | None = None
    ) -> GridPoint:
        """`pinned` is `feeds.nws.grid_point` (e.g. `"EWX/156,91"`), which skips the lookup entirely
        when set (02b S2.4). Result is cached for the process lifetime."""
        if self._grid_point is not None:
            return self._grid_point
        if pinned is not None:
            office, xy = pinned.split("/")
            x_str, y_str = xy.split(",")
            self._grid_point = GridPoint(office=office, x=int(x_str), y=int(y_str))
            return self._grid_point
        if lat is None or lon is None:
            raise ValueError("either `pinned` or both `lat`/`lon` are required")
        response = await request_with_retry(
            self.http_client, "GET", f"{self.base_url}/points/{lat},{lon}", headers=self._headers()
        )
        props = response.json()["properties"]
        self._grid_point = GridPoint(office=props["gridId"], x=props["gridX"], y=props["gridY"])
        return self._grid_point

    async def hourly_forecast(self, grid_point: GridPoint, *, recorded_at: datetime) -> list[FeedObs] | None:
        """Returns `None` on a `304 Not Modified` (caller keeps its cached value and only updates the
        age counter, per 02b S2.4)."""
        headers = self._headers()
        if self._last_updated is not None:
            headers["If-Modified-Since"] = format_datetime(self._last_updated, usegmt=True)

        url = f"{self.base_url}/gridpoints/{grid_point.office}/{grid_point.x},{grid_point.y}/forecast/hourly"
        response = await self.http_client.request("GET", url, headers=headers, timeout=10.0)
        if response.status_code == 304:
            return None
        if response.status_code >= 400:
            raise FeedHttpError(response.status_code, f"NWS hourly forecast -> {response.status_code}")

        payload = response.json()
        updated_str = payload["properties"]["updated"]
        self._last_updated = datetime.fromisoformat(updated_str)
        return nws_forecast_to_feed_obs(payload, recorded_at=recorded_at)
