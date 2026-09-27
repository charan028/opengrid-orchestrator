"""Wires together config, anomaly store, token store and data generator into
one object attached to `app.state.runtime`, plus the shared HTTP-anomaly
check used by every data route."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from ogsim.market.anomalies import AnomalyStore
from ogsim.market.as_dispatch import AsDispatchScenarios, mms_state_from_env
from ogsim.market.config import MarketConfig, load_config
from ogsim.market.data import MarketData
from ogsim.market.security import TokenStore

DEFAULT_5XX_STATUS = 503
HTTP_429_STATUS = 429
HTTP_401_STATUS = 401
DEFAULT_RETRY_AFTER_S = 30
DEFAULT_SLOW_LATENCY_S = 5.0
MALFORMED_PAYLOAD_BODY = b'{"data": [ this is not valid json,,, }'


class MarketRuntime:
    """Holds all per-process state for ogsim.market. `clock` is injectable so
    tests can pin "now" instead of depending on wall-clock time."""

    def __init__(self, cfg: MarketConfig | None = None, clock: Callable[[], datetime] | None = None):
        self.cfg = cfg or load_config()
        self.anomalies = AnomalyStore()
        self.tokens = TokenStore(self.cfg)
        self.data = MarketData(self.cfg, self.anomalies)
        self._clock = clock or (lambda: datetime.now(UTC))
        # D-35: the MMS/EWS dispatch-instruction endpoint mounted at /mms, fed by AS dispatch anomalies.
        self.mms = mms_state_from_env(self.cfg.seed, self.now)
        self.as_dispatch = AsDispatchScenarios.from_env(self.mms.dispatch)

    def now(self) -> datetime:
        return self._clock()


def get_runtime(request: Request) -> MarketRuntime:
    runtime: MarketRuntime = request.app.state.runtime
    return runtime


async def http_anomaly_gate(request: Request, product: str) -> Response | None:
    """Checks outage/throttle/malformed/latency anomalies for `product`
    (or '*'). Returns a Response to short-circuit the route, or None to
    continue normally (after sleeping off any injected latency)."""
    rt = get_runtime(request)
    now = rt.now().timestamp()
    active = rt.anomalies.active(product, now)
    for a in active:
        if a.type == "http_5xx":
            status = int(a.params.get("status", DEFAULT_5XX_STATUS))
            return JSONResponse(status_code=status, content={"error": "simulated_outage", "anomaly_id": a.id})
        if a.type == "http_429":
            retry_after = int(a.params.get("retry_after_s", DEFAULT_RETRY_AFTER_S))
            return JSONResponse(
                status_code=HTTP_429_STATUS,
                headers={"Retry-After": str(retry_after)},
                content={"error": "rate_limited", "anomaly_id": a.id},
            )
        if a.type == "malformed_payload":
            return Response(content=MALFORMED_PAYLOAD_BODY, media_type="application/json", status_code=200)
    latency = max(
        (
            float(a.params.get("latency_s", DEFAULT_SLOW_LATENCY_S))
            for a in active
            if a.type == "slow_response"
        ),
        default=0.0,
    )
    if latency > 0:
        await asyncio.sleep(latency)
    return None


def key_gate(request: Request, product: str, key_used_kind: str | None) -> Response | None:
    """401 on the primary key only, to exercise key rotation."""
    rt = get_runtime(request)
    now = rt.now().timestamp()
    for a in rt.anomalies.active(product, now):
        if a.type == "http_401_primary" and key_used_kind == "primary":
            return JSONResponse(
                status_code=HTTP_401_STATUS, content={"error": "unauthorized", "anomaly_id": a.id}
            )
    return None
