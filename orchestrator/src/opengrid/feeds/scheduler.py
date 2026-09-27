"""og-feeds scheduler (02b S2.5-2.7): one polling loop per source, each with its own cadence, token
bucket (ERCOT only) and circuit breaker; writes `feed_obs`/`feed_status` and traces feed-quality
changes/key rotations as `event_class="FEED_CHANGE"`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from opengrid.feeds.breaker import CircuitBreaker
from opengrid.feeds.eia import EiaClient
from opengrid.feeds.ercot import PRODUCT_PATHS, ErcotAuthError, ErcotClient
from opengrid.feeds.http_client import FeedHttpError
from opengrid.feeds.normalize import FeedDataError
from opengrid.feeds.nws import NwsClient
from opengrid.feeds.staleness import threshold_s_for_product
from opengrid.feeds.store import FeedStore
from opengrid.feeds.token_bucket import TokenBucket
from opengrid.trace.store import TraceStore

logger = logging.getLogger(__name__)

# Poll cadences (02b S2.2 table). AS MCPC is nominally "once daily at 14:00 CT"; MVP-S polls it on a
# fixed interval rather than aligning to that clock time (documented simplification, see feeds/README).
POLL_INTERVAL_S: dict[str, float] = {
    "np6-905-cd": 5 * 60,
    "np6-345-cd": 60 * 60,
    "np4-732-cd": 30 * 60,
    "np4-737-cd": 30 * 60,
    "np4-188-cd": 24 * 60 * 60,
    "np4-745-cd": 30 * 60,  # D-28 regional solar: hourly posting, polled like NP4-737-CD
}
NWS_POLL_INTERVAL_S = 60 * 60
# A failed or skipped (breaker open) ERCOT poll is retried sooner than the product's normal cadence:
# 5 min, doubling per consecutive failure, capped at 60 min (and never later than the normal cadence).
# Without this a single miss of the daily NP4-188-CD poll held its data stale for ~24 h.
FAILURE_RETRY_BASE_S = 5 * 60
FAILURE_RETRY_CAP_S = 60 * 60
EIA_FALLBACK_PRODUCT = "np6-345-cd"  # the only product EIA can stand in for (system load)

FEED_CHANGE_STREAM = "feeds"


@dataclass
class _ProductState:
    next_poll_at: datetime
    was_stale: bool = False
    consecutive_failures: int = 0


def next_poll_delay_s(product: str, consecutive_failures: int) -> float:
    """Seconds until `product` is polled again: its normal cadence after a success, otherwise the
    capped exponential retry delay (`FAILURE_RETRY_BASE_S` .. `FAILURE_RETRY_CAP_S`), never longer
    than the normal cadence."""
    interval = POLL_INTERVAL_S[product]
    if consecutive_failures <= 0:
        return interval
    retry = float(min(FAILURE_RETRY_CAP_S, FAILURE_RETRY_BASE_S * 2 ** (consecutive_failures - 1)))
    return min(interval, retry)


@dataclass
class FeedsScheduler:
    ercot: ErcotClient
    eia: EiaClient
    nws: NwsClient
    store: FeedStore
    staleness_cfg: dict[str, float]
    nws_grid_point_pinned: str | None
    ercot_bucket: TokenBucket
    trace: TraceStore | None = None
    #: ERCOT products configured off (e.g. `[feeds.ercot].solar_by_region_enabled = false`): never polled,
    #: so they never write a `feed_status` row that health could read as stale.
    disabled_products: frozenset[str] = frozenset()
    #: `[feeds.ercot].products`: the ERCOT products to poll (None = every known product). A product not
    #: listed is never polled, whatever its own enable flag says; `disabled_products` then removes more.
    products: tuple[str, ...] | None = None
    _ercot_breaker: CircuitBreaker = field(default_factory=lambda: CircuitBreaker("ERCOT"))
    _eia_breaker: CircuitBreaker = field(default_factory=lambda: CircuitBreaker("EIA"))
    _nws_breaker: CircuitBreaker = field(default_factory=lambda: CircuitBreaker("NWS"))
    _product_state: dict[str, _ProductState] = field(default_factory=dict)
    _nws_state: _ProductState = field(init=False)
    _eia_fallback_active: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        epoch = datetime.min.replace(tzinfo=UTC)
        wanted = tuple(PRODUCT_PATHS) if self.products is None else self.products
        unknown = sorted(set(wanted) - set(PRODUCT_PATHS))
        if unknown:
            raise ValueError(f"[feeds.ercot].products lists unknown ERCOT products: {unknown}")
        for product in wanted:
            if product not in self.disabled_products:
                self._product_state[product] = _ProductState(next_poll_at=epoch)
        self._nws_state = _ProductState(next_poll_at=epoch)

    async def _trace(self, event_class: str, payload: dict[str, object]) -> None:
        if self.trace is None:
            logger.info("feed_change (trace disabled)", extra={"event_class": event_class, **payload})
            return
        await self.trace.append(FEED_CHANGE_STREAM, "FEED_CHANGE", event_class, payload)

    async def run_cycle(self, *, now: datetime | None = None) -> None:
        """One scheduler tick: poll every source whose cadence is due. Called by `run_forever` (02b
        S1.2) at a short fixed interval (e.g. 5s) -- `POLL_INTERVAL_S`/`NWS_POLL_INTERVAL_S` govern
        which sources actually make a network call on a given tick."""
        now = now or datetime.now(UTC)
        for product, state in self._product_state.items():
            if now >= state.next_poll_at:
                ok = await self._poll_ercot_product(product, now=now)
                state.consecutive_failures = 0 if ok else state.consecutive_failures + 1
                delay_s = next_poll_delay_s(product, state.consecutive_failures)
                state.next_poll_at = now.replace(microsecond=0) + timedelta(seconds=delay_s)

        if now >= self._nws_state.next_poll_at:
            await self._poll_nws(now=now)
            self._nws_state.next_poll_at = now.replace(microsecond=0) + timedelta(seconds=NWS_POLL_INTERVAL_S)

    async def _poll_ercot_product(self, product: str, *, now: datetime) -> bool:
        """Poll one ERCOT product; True only if data was fetched and stored (False when the breaker
        skipped the request or it failed -- the caller then retries with backoff)."""
        if not self._ercot_breaker.allow_request(now=now):
            if product == EIA_FALLBACK_PRODUCT:
                await self._poll_eia_fallback(now=now)
            return False
        if self._ercot_breaker.state == "HALF_OPEN":
            self._ercot_breaker.begin_half_open_probe()

        await self.ercot_bucket.acquire()
        try:
            obs, rotation_events = await self.ercot.fetch_product(product, now=now)
        except (FeedHttpError, FeedDataError, ErcotAuthError) as exc:
            logger.warning("ERCOT poll failed", extra={"product": product, "error": str(exc)})
            was_open = self._ercot_breaker.is_open
            self._ercot_breaker.record_failure(now=now)
            await self.store.update_status(
                source="ERCOT",
                product=product,
                consecutive_failures=self._ercot_breaker.consecutive_failures,
                breaker_open=self._ercot_breaker.is_open,
                active_key=self.ercot.active_key,
            )
            if self._ercot_breaker.is_open and not was_open:
                await self._trace("BREAKER_OPEN", {"source": "ERCOT", "product": product, "reason": str(exc)})
            if product == EIA_FALLBACK_PRODUCT and self._ercot_breaker.is_open:
                await self._poll_eia_fallback(now=now)
            return False

        self._ercot_breaker.record_success()
        for rotation in rotation_events:
            await self._trace(
                "KEY_ROTATION",
                {
                    "source": "ERCOT",
                    "from_key": rotation.from_key,
                    "to_key": rotation.to_key,
                    "reason": rotation.reason,
                },
            )
        await self.store.upsert_obs(obs)
        last_value_at = max((row.ts for row in obs), default=None)
        await self.store.update_status(
            source="ERCOT",
            product=product,
            last_value_at=last_value_at,
            last_success_at=now,
            consecutive_failures=0,
            breaker_open=False,
            active_key=self.ercot.active_key,
        )
        if product == EIA_FALLBACK_PRODUCT and self._eia_fallback_active:
            self._eia_fallback_active = False
            await self._trace("EIA_FALLBACK_DISENGAGED", {"reason": "ercot_recovered"})
        await self._check_staleness_transition("ERCOT", product, last_value_at, now=now)
        return True

    async def _poll_eia_fallback(self, *, now: datetime) -> None:
        if not self._eia_breaker.allow_request(now=now):
            return
        try:
            obs = await self.eia.hourly_demand(recorded_at=now)
        except (FeedHttpError, FeedDataError) as exc:
            logger.warning("EIA fallback poll failed", extra={"error": str(exc)})
            self._eia_breaker.record_failure(now=now)
            await self.store.update_status(
                source="EIA",
                product="eia-demand",
                consecutive_failures=self._eia_breaker.consecutive_failures,
                breaker_open=self._eia_breaker.is_open,
            )
            return

        self._eia_breaker.record_success()
        await self.store.upsert_obs(obs)
        last_value_at = max((row.ts for row in obs), default=None)
        await self.store.update_status(
            source="EIA",
            product="eia-demand",
            last_value_at=last_value_at,
            last_success_at=now,
            consecutive_failures=0,
            breaker_open=False,
        )
        if not self._eia_fallback_active:
            self._eia_fallback_active = True
            await self._trace("EIA_FALLBACK_ENGAGED", {"reason": "ercot_breaker_open"})

    async def _poll_nws(self, *, now: datetime) -> None:
        if not self._nws_breaker.allow_request(now=now):
            return
        try:
            grid_point = await self.nws.resolve_grid_point(pinned=self.nws_grid_point_pinned)
            obs = await self.nws.hourly_forecast(grid_point, recorded_at=now)
        except (FeedHttpError, FeedDataError) as exc:
            logger.warning("NWS poll failed", extra={"error": str(exc)})
            self._nws_breaker.record_failure(now=now)
            await self.store.update_status(
                source="NWS",
                product="nws-hourly",
                consecutive_failures=self._nws_breaker.consecutive_failures,
                breaker_open=self._nws_breaker.is_open,
            )
            return

        self._nws_breaker.record_success()
        if obs is not None:
            await self.store.upsert_obs(obs)
            last_value_at = max((row.ts for row in obs), default=None)
            await self.store.update_status(
                source="NWS",
                product="nws-hourly",
                last_value_at=last_value_at,
                last_success_at=now,
                consecutive_failures=0,
                breaker_open=False,
            )
            await self._check_staleness_transition("NWS", "nws-hourly", last_value_at, now=now)
        else:
            await self.store.update_status(
                source="NWS",
                product="nws-hourly",
                last_success_at=now,
                consecutive_failures=0,
                breaker_open=False,
            )

    async def _check_staleness_transition(
        self, source: str, product: str, last_value_at: datetime | None, *, now: datetime
    ) -> None:
        if last_value_at is None:
            return
        threshold_s = threshold_s_for_product(source, product, self.staleness_cfg)
        age_s = (now - last_value_at).total_seconds()
        is_stale_now = age_s > threshold_s
        state = self._product_state.get(product) or self._nws_state
        if state is None or is_stale_now == state.was_stale:
            return
        state.was_stale = is_stale_now
        await self._trace(
            "STALE" if is_stale_now else "FRESH",
            {"source": source, "product": product, "age_s": age_s, "threshold_s": threshold_s},
        )
