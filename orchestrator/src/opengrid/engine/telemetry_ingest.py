"""Telemetry ingest decoupling, step 1 (r3.4.5, behind `[ingest].telemetry_decoupled`, default OFF): parse and validate hub telemetry OFF the event loop.

Until r3.4.3 every `<root>/tel/#` message was JSON-decoded, schema-validated (jsonschema) and parsed (pydantic)
on the engine's asyncio loop, inline in the MQTT ingest loop -- at fleet scale the single largest share of
loop time outside the dispatch tick, and a burst of telemetry delayed the tick itself.

Now:

- `submit(raw)` (the MQTT loop) only appends the raw bytes to a bounded deque and wakes the parser thread.
  When the deque is full the OLDEST raw message is dropped (counted): a newer sample of the same hub is
  already behind it, and telemetry is state, not an event log.
- One parser thread decodes, validates and parses, then stores the result in a **latest-value-per-hub**
  buffer: newest `ts` wins (an out-of-order older sample is dropped, counted); a newer sample supersedes
  an unapplied one (counted). The buffer holds at most one entry per hub, capped at `max_hubs`.
- `run_applier()` (an asyncio task) swaps the buffer out every `apply_interval_s` (default 100 ms) and
  applies each hub's latest sample to the fleet twin (`fleet.apply_telemetry`), on the loop, where the twin
  lives -- the twin is never touched from the thread.

Memory is bounded (raw deque `raw_max` + one entry per hub); total CPU is unchanged (the same decode /
validate / parse, now on another thread; applying is a few attribute writes per hub). Latency added to a
sample is at most one apply interval. Step 2 (a separate og-ingest process) is a design only
(docs/orchestrator/ingest-decoupling.md).
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger("opengrid.engine")

DEFAULT_RAW_MAX = 20_000
DEFAULT_MAX_HUBS = 100_000
DEFAULT_APPLY_INTERVAL_S = 0.1
#: Yield to the loop after this many applied samples in one pass (a full 7,500-hub pass stays responsive).
APPLY_YIELD_EVERY = 500

#: `(payload) -> parsed`: validates (raises on an invalid payload) and returns an object with `hub_id`, `ts`.
Parse = Callable[[dict[str, Any]], Any]
Apply = Callable[[Any], Awaitable[None]]


@dataclass(slots=True)
class DecouplerStats:
    received: int = 0
    raw_dropped: int = 0  # the raw deque was full: the oldest raw message was dropped
    invalid: int = 0
    stale: int = 0  # an older sample than the one already buffered for that hub
    superseded: int = 0  # a buffered, not yet applied sample replaced by a newer one
    hub_cap_dropped: int = 0
    applied: int = 0
    apply_failed: int = 0


@dataclass(frozen=True, slots=True)
class _Latest:
    hub_id: str
    parsed: Any
    ts: datetime
    payload_ts: Any


class TelemetryDecoupler:
    """Raw telemetry in (`submit`, on the loop), parsed latest-per-hub out (`run_applier`, on the loop), with the
    decode/validate/parse on one daemon thread in between (see the module docstring)."""

    def __init__(
        self,
        parse: Parse,
        apply: Apply,
        *,
        observe: Callable[[Any], None] | None = None,
        raw_max: int = DEFAULT_RAW_MAX,
        max_hubs: int = DEFAULT_MAX_HUBS,
        apply_interval_s: float = DEFAULT_APPLY_INTERVAL_S,
    ) -> None:
        self._parse = parse
        self._apply = apply
        self._observe = observe
        self._raw: collections.deque[bytes | str] = collections.deque(maxlen=raw_max)
        self._raw_max = raw_max
        self._max_hubs = max_hubs
        self.apply_interval_s = apply_interval_s
        self._latest: dict[str, _Latest] = {}
        #: per hub, the `ts` of the sample last applied to the twin (an older late arrival is stale).
        self._applied_ts: dict[str, datetime] = {}
        self._cond = threading.Condition()
        self._lock = threading.Lock()  # guards `_latest` and `stats`
        self._stop = False
        self._thread: threading.Thread | None = None
        self.stats = DecouplerStats()

    # ------------------------------------------------------------------ loop side
    def submit(self, raw: bytes | str) -> None:
        """Never blocks on parsing: append and wake the parser (the oldest raw message goes when full)."""
        with self._cond:
            if len(self._raw) >= self._raw_max:
                self.stats.raw_dropped += 1
                if self.stats.raw_dropped == 1 or self.stats.raw_dropped % 1000 == 0:
                    logger.warning(
                        "telemetry raw backlog full; dropping the oldest",
                        extra={"dropped": self.stats.raw_dropped},
                    )
            self._raw.append(raw)
            self.stats.received += 1
            self._cond.notify()

    def drain(self) -> list[_Latest]:
        """Take every hub's latest parsed sample (the buffer is empty afterwards)."""
        with self._lock:
            latest, self._latest = self._latest, {}
        return list(latest.values())

    def backlog(self) -> tuple[int, int]:
        """(raw messages waiting for the parser, parsed hubs waiting to be applied)."""
        with self._cond:
            raw = len(self._raw)
        with self._lock:
            return raw, len(self._latest)

    async def apply_once(self) -> int:
        items = self.drain()
        for n, item in enumerate(items, 1):
            try:
                if self._observe is not None:
                    self._observe(item.payload_ts)
                await self._apply(item.parsed)
                with self._lock:
                    self.stats.applied += 1
                    if item.hub_id in self._applied_ts or len(self._applied_ts) < self._max_hubs:
                        self._applied_ts[item.hub_id] = item.ts
            except Exception:
                self.stats.apply_failed += 1
                logger.exception("telemetry apply failed")
            if n % APPLY_YIELD_EVERY == 0:
                await asyncio.sleep(0)
        return len(items)

    async def run_applier(self, *, sleep: Callable[[float], Awaitable[object]] = asyncio.sleep) -> None:
        while True:
            started = time.monotonic()
            await self.apply_once()
            await sleep(max(0.0, self.apply_interval_s - (time.monotonic() - started)))

    # ------------------------------------------------------------------ parser thread
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop = False
        self._thread = threading.Thread(target=self._run_parser, name="og-telemetry-parse", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 2.0) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout_s)
            self._thread = None

    def _run_parser(self) -> None:
        while True:
            with self._cond:
                while not self._raw and not self._stop:
                    self._cond.wait()
                if self._stop:
                    return
                batch = list(self._raw)
                self._raw.clear()
            for raw in batch:
                self.parse_one(raw)

    def parse_one(self, raw: bytes | str) -> None:
        """Decode, validate, parse and store one raw message (the parser thread; tests call it directly)."""
        try:
            payload = json.loads(raw)
            parsed = self._parse(payload)
            hub_id, ts = str(parsed.hub_id), parsed.ts
        except Exception:
            with self._lock:
                self.stats.invalid += 1
                invalid = self.stats.invalid
            if invalid == 1 or invalid % 1000 == 0:
                logger.warning("dropped invalid telemetry payload", extra={"invalid": invalid})
            return
        entry = _Latest(hub_id, parsed, ts, payload.get("ts") if isinstance(payload, dict) else None)
        with self._lock:
            applied = self._applied_ts.get(hub_id)
            if applied is not None and ts < applied:
                self.stats.stale += 1  # older than what the twin already has
                return
            held = self._latest.get(hub_id)
            if held is not None:
                if ts < held.ts:
                    self.stats.stale += 1
                    return
                self.stats.superseded += 1
            elif len(self._latest) >= self._max_hubs:
                self.stats.hub_cap_dropped += 1
                return
            self._latest[hub_id] = entry
