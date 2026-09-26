"""EIA Open Data v2 client (02b S2.3): standby fallback for system load, used only while the ERCOT
circuit breaker is open for the load product. Never polled as primary in MVP-S.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import httpx

from opengrid.core.models.platform import FeedObs
from opengrid.feeds.http_client import FeedHttpError, request_with_retry
from opengrid.feeds.normalize import eia_demand_to_feed_obs
from opengrid.feeds.secrets import Secret, mask_secrets
from opengrid.platform.config import resolve_secret

_DEMAND_PATH = "/electricity/rto/region-data/data/"


@dataclass
class EiaClient:
    base_url: str
    api_key_env: str
    respondent: str
    http_client: httpx.AsyncClient

    async def hourly_demand(self, *, recorded_at: datetime, length: int = 24) -> list[FeedObs]:
        """`GET /electricity/rto/region-data/data/` for `self.respondent`'s hourly demand (`type=D`).

        Unlike ERCOT (bearer token + subscription key, both sent as headers), the EIA v2 API only
        documents `api_key` as a query parameter -- it ends up in the request URL, not a header. A
        `Secret` wrapper keeps the raw value out of local variables, and any `FeedHttpError` raised by
        `request_with_retry` (whose message embeds the URL it just called) has the key masked out
        before it can reach a log line (BUILD.md S6 "never print, log or commit secret values"; live
        defect: the key was previously visible in plain text both in such an error message and via
        httpx's own per-request INFO log, which `feeds.http_client` now quiets for this reason)."""
        api_key = Secret(resolve_secret(self.api_key_env))
        try:
            response = await request_with_retry(
                self.http_client,
                "GET",
                f"{self.base_url}{_DEMAND_PATH}",
                params={
                    "api_key": api_key.reveal(),
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
        except FeedHttpError as exc:
            raise FeedHttpError(exc.status_code, mask_secrets(str(exc), (api_key,))) from None
        return eia_demand_to_feed_obs(response.json(), recorded_at=recorded_at)
