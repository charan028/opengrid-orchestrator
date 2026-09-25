"""In-process anomaly store for ogsim.market.

Anomalies are {id, type, target, params, start, duration} exactly per
BUILD.md §3. `target` selects which product(s) the anomaly applies to:
one of the ERCOT product ids ("np6-905-cd", "np6-345-cd", "np4-732-cd",
"np4-737-cd", "np4-188-cd"), "eia", "nws", or "*" for every market product.

Market anomaly types implemented here:
  price_spike        params: {value_usd_per_mwh}            -> SPP override
  negative_price      params: {value_usd_per_mwh (<0)}        -> SPP override
  as_price_jump       params: {service (RegUp|RegDown|RRS|NonSpin|ECRS), value_usd_per_mwh}
  http_5xx            params: {status (default 503)}          -> outage
  http_429            params: {retry_after_s (default 30)}    -> throttling
  http_401_primary    params: {}                               -> 401 iff caller used the primary key
  stale_posting       params: {}                               -> data stops advancing
  malformed_payload    params: {}                               -> intentionally broken JSON body
  slow_response        params: {latency_s (default 5)}          -> added latency
  nws_extreme_weather  params: {condition, temperature_c, wind_kph} -> NWS forecast override
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

DEFAULT_DURATION_S = 60.0
EXPIRY_GRACE_PERIOD_S = 3600.0  # how long an expired anomaly is kept around before being swept

MARKET_ANOMALY_TYPES = {
    "price_spike",
    "negative_price",
    "as_price_jump",
    "http_5xx",
    "http_429",
    "http_401_primary",
    "stale_posting",
    "malformed_payload",
    "slow_response",
    "nws_extreme_weather",
}


@dataclass
class Anomaly:
    id: str
    type: str
    target: str
    params: dict[str, Any] = field(default_factory=dict)
    start: float = 0.0  # unix epoch seconds
    duration: float = DEFAULT_DURATION_S  # seconds

    def active_at(self, now: float) -> bool:
        return self.start <= now < (self.start + self.duration)

    def applies_to(self, product: str) -> bool:
        return self.target in ("*", product)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "target": self.target,
            "params": self.params,
            "start": self.start,
            "duration": self.duration,
        }


class AnomalyStore:
    """Thread-safe registry of injected anomalies, keyed by id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._anomalies: dict[str, Anomaly] = {}

    def inject(self, anomaly: Anomaly) -> Anomaly:
        with self._lock:
            self._anomalies[anomaly.id] = anomaly
        return anomaly

    def cancel(self, anomaly_id: str) -> bool:
        with self._lock:
            return self._anomalies.pop(anomaly_id, None) is not None

    def all(self) -> list[Anomaly]:
        with self._lock:
            return list(self._anomalies.values())

    def active(self, product: str | None = None, now: float | None = None) -> list[Anomaly]:
        now = time.time() if now is None else now
        with self._lock:
            items = list(self._anomalies.values())
        return [a for a in items if a.active_at(now) and (product is None or a.applies_to(product))]

    def sweep_expired(self, now: float | None = None) -> None:
        """Drop anomalies whose window ended more than an hour ago, so the
        store doesn't grow unbounded across a long-running process."""
        now = time.time() if now is None else now
        with self._lock:
            expired = [
                aid
                for aid, a in self._anomalies.items()
                if now > (a.start + a.duration + EXPIRY_GRACE_PERIOD_S)
            ]
            for aid in expired:
                del self._anomalies[aid]
