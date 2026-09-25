"""EIA Open Data v2 client (02b S2.3): standby fallback for system load, used only while the ERCOT
circuit breaker is open for the load product. Never polled as primary in MVP-S.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import httpx

from opengrid.core.models.platform import FeedObs
from opengrid.feeds.http_client import request_with_retry
from opengrid.feeds.normalize import eia_demand_to_feed_obs
from opengrid.platform.config import resolve_secret

_DEMAND_PATH = "/electricity/rto/region-data/data/"


@dataclass
class EiaClient:
    base_url: str
    api_key_env: str
    respondent: str
    http_client: httpx.AsyncClient

    async def hourly_demand(self, *, recorded_at: datetime, length: int = 24) -> list[FeedObs]:
        """`GET /electricity/rto/region-data/data/` for `self.respondent`'s hourly demand (`type=D`)."""
        api_key = resolve_secret(self.api_key_env)
        response = await request_with_retry(
            self.http_client,
            "GET",
            f"{self.base_url}{_DEMAND_PATH}",
            params={
                "api_key": api_key,
                "frequency": "hourly",
                "data[]": "value",
                "facets[respondent][]": self.respondent,
                "facets[type][]": "D",
                "sort[0][column]": "period",
                "sort[0][direction]": "desc",
                "offset": "0",
                "length": str(length),
            },
        )
        return eia_demand_to_feed_obs(response.json(), recorded_at=recorded_at)
