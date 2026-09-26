"""D-28 solar share measured: NP4-745-CD regional solar ingestion (through the existing ERCOT client, auth
and scheduler) and the `opengrid.feeds.solar_share` read helper, against a fake HTTP transport."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from opengrid.core.models.platform import FeedObs
from opengrid.feeds import disabled_ercot_products
from opengrid.feeds.ercot import PRODUCT_PATHS, SOLAR_BY_REGION_PRODUCT, ErcotClient, KeyRotationEvent
from opengrid.feeds.normalize import (
    SOLAR_REGIONS,
    FeedDataError,
    ercot_solar_by_region_to_feed_obs,
    solar_actual_series,
    solar_forecast_series,
)
from opengrid.feeds.scheduler import POLL_INTERVAL_S, FeedsScheduler
from opengrid.feeds.solar_share import (
    SYSTEM_ZONE,
    hour_start,
    solar_share_of_load,
    solar_share_ratio,
    zone_regions_from_config,
)
from opengrid.feeds.staleness import threshold_s_for_product
from opengrid.feeds.token_bucket import TokenBucket

NOW = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)
RECORDED_AT = NOW

_BASE_FIELDS = ["postedDatetime", "deliveryDate", "hourEnding", "genSystemWide", "STPPFSystemWide"]


def _regional_payload(
    rows: list[list[object]], regions: tuple[str, ...] = SOLAR_REGIONS
) -> dict[str, object]:
    names = list(_BASE_FIELDS)
    for region in regions:
        names += [f"gen{region}", f"COPHSL{region}", f"STPPF{region}", f"PVGRPP{region}"]
    names.append("DSTFlag")
    return {"fields": [{"name": n} for n in names], "data": rows}


def _row(hour_ending: int, actual: float | None, forecast: float, regions: int = 6) -> list[object]:
    row: list[object] = ["2026-09-26T12:55:00", "2026-09-26", hour_ending, 9000.0, 9500.0]
    for i in range(regions):
        row += [None if actual is None else actual + i, 0.0, forecast + i, 0.0]
    row.append(False)
    return row


# --- normalizer --------------------------------------------------------------------------------------


def test_regional_normalizer_writes_actual_and_forecast_per_region() -> None:
    obs = ercot_solar_by_region_to_feed_obs(
        _regional_payload([_row(13, 100.0, 110.0)]), product=SOLAR_BY_REGION_PRODUCT, recorded_at=RECORDED_AT
    )
    series = {o.series: o.value for o in obs}
    assert series[solar_actual_series("CenterWest")] == 100.0
    assert series[solar_forecast_series("CenterWest")] == 110.0
    assert series[solar_actual_series("CenterEast")] == 105.0
    assert len(obs) == 2 * len(SOLAR_REGIONS)
    # System-wide columns are NP4-737-CD's to store, never duplicated from this product.
    assert not any(o.series in ("solar_actual", "solar_forecast") for o in obs)
    # Hour-ending 13 America/Chicago (CDT) = 12:00 local start = 17:00 UTC.
    assert {o.ts for o in obs} == {datetime(2026, 9, 26, 17, 0, tzinfo=UTC)}
    assert all(o.product == SOLAR_BY_REGION_PRODUCT and o.unit == "mw" for o in obs)


def test_regional_normalizer_skips_actual_for_forecast_horizon_rows() -> None:
    obs = ercot_solar_by_region_to_feed_obs(
        _regional_payload([_row(20, None, 50.0)]), product=SOLAR_BY_REGION_PRODUCT, recorded_at=RECORDED_AT
    )
    assert all(o.series.startswith("solar_forecast_") for o in obs)
    assert len(obs) == len(SOLAR_REGIONS)


def test_regional_normalizer_uses_only_regions_present_in_fields() -> None:
    payload = _regional_payload([_row(13, 100.0, 110.0, regions=2)], regions=("FarWest", "NorthWest"))
    obs = ercot_solar_by_region_to_feed_obs(payload, product=SOLAR_BY_REGION_PRODUCT, recorded_at=RECORDED_AT)
    assert {o.series for o in obs} == {
        solar_actual_series("FarWest"),
        solar_forecast_series("FarWest"),
        solar_actual_series("NorthWest"),
        solar_forecast_series("NorthWest"),
    }


def test_regional_normalizer_raises_when_no_region_columns() -> None:
    payload = _regional_payload([], regions=())
    with pytest.raises(FeedDataError, match="no solar region columns"):
        ercot_solar_by_region_to_feed_obs(payload, product=SOLAR_BY_REGION_PRODUCT, recorded_at=RECORDED_AT)


# --- ERCOT client / config / cadence ------------------------------------------------------------------


@pytest.fixture
def _ercot_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_ERCOT_USER", "user@example.com")
    monkeypatch.setenv("TEST_ERCOT_PASSWORD", "pw")
    monkeypatch.setenv("TEST_ERCOT_KEY_PRIMARY", "primary-key")
    monkeypatch.setenv("TEST_ERCOT_KEY_SECONDARY", "secondary-key")


@pytest.mark.asyncio
@pytest.mark.usefixtures("_ercot_env")
async def test_client_fetches_regional_product_with_posted_datetime_window() -> None:
    seen: dict[str, httpx.URL] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith("http://test/token"):
            return httpx.Response(200, json={"id_token": "tok"})
        seen["url"] = request.url
        return httpx.Response(200, json=_regional_payload([_row(13, 100.0, 110.0)]))

    client = ErcotClient(
        base_url="http://test/ercot",
        username_env="TEST_ERCOT_USER",
        password_env="TEST_ERCOT_PASSWORD",
        primary_key_env="TEST_ERCOT_KEY_PRIMARY",
        secondary_key_env="TEST_ERCOT_KEY_SECONDARY",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        token_url="http://test/token",
    )
    obs, _events = await client.fetch_product(SOLAR_BY_REGION_PRODUCT, now=NOW)

    assert seen["url"].path == "/ercot/np4-745-cd/spp_hrly_actual_fcast_geo"
    assert "postedDatetimeFrom" in seen["url"].params
    assert "postedDatetimeTo" in seen["url"].params
    assert len(obs) == 2 * len(SOLAR_REGIONS)


def test_regional_product_cadence_and_freshness_match_hourly_publication() -> None:
    assert SOLAR_BY_REGION_PRODUCT in PRODUCT_PATHS
    assert POLL_INTERVAL_S[SOLAR_BY_REGION_PRODUCT] == POLL_INTERVAL_S["np4-737-cd"]
    cfg = {"wind_solar_fresh_s": 10800}
    assert threshold_s_for_product("ERCOT", SOLAR_BY_REGION_PRODUCT, cfg) == 10800.0


def test_disabled_ercot_products_switch() -> None:
    assert disabled_ercot_products({}) == frozenset()
    assert disabled_ercot_products({"solar_by_region_enabled": True}) == frozenset()
    assert disabled_ercot_products({"solar_by_region_enabled": False}) == frozenset({SOLAR_BY_REGION_PRODUCT})


@dataclass
class _FakeErcot:
    active_key: str = "PRIMARY"
    polled: list[str] = field(default_factory=list)

    async def fetch_product(
        self, product: str, *, now: datetime
    ) -> tuple[list[FeedObs], list[KeyRotationEvent]]:
        self.polled.append(product)
        return [], []


@dataclass
class _FakeStore:
    async def upsert_obs(self, rows: list[FeedObs]) -> None:
        return None

    async def update_status(self, **kwargs: object) -> None:
        return None


class _FakeNws:
    async def resolve_grid_point(self, *, pinned: str | None) -> object:
        return object()

    async def hourly_forecast(self, grid_point: object, *, recorded_at: datetime) -> list[FeedObs] | None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", [frozenset(), frozenset({SOLAR_BY_REGION_PRODUCT})])
async def test_scheduler_polls_regional_product_unless_disabled(disabled: frozenset[str]) -> None:
    ercot = _FakeErcot()
    scheduler = FeedsScheduler(
        ercot=ercot,  # type: ignore[arg-type]
        eia=object(),  # type: ignore[arg-type]
        nws=_FakeNws(),  # type: ignore[arg-type]
        store=_FakeStore(),  # type: ignore[arg-type]
        staleness_cfg={},
        nws_grid_point_pinned=None,
        ercot_bucket=TokenBucket(capacity=100, refill_per_s=100),
        disabled_products=disabled,
    )
    await scheduler.run_cycle(now=NOW)
    assert (SOLAR_BY_REGION_PRODUCT in ercot.polled) is (not disabled)
    assert "np4-737-cd" in ercot.polled


# --- solar share read helper ---------------------------------------------------------------------------


def _obs(series: str, ts: datetime, value: float) -> FeedObs:
    return FeedObs(
        source="ERCOT",
        product="p",
        series=series,
        ts=ts,
        value=value,
        unit="mw",
        quality="GOOD",
        recorded_at=ts,
    )


class _FakeWindow:
    def __init__(self, values: dict[str, float], ts: datetime) -> None:
        self._values = values
        self._ts = ts

    async def __call__(self, series: str, t0: datetime, t1: datetime) -> list[FeedObs]:
        if series in self._values and t0 <= self._ts < t1:
            return [_obs(series, self._ts, self._values[series])]
        return []


HOUR = datetime(2026, 9, 26, 17, 0, tzinfo=UTC)


def test_solar_share_ratio() -> None:
    assert solar_share_ratio(20_000.0, 50_000.0) == pytest.approx(0.4)
    assert solar_share_ratio(-5.0, 40_000.0) == 0.0  # night-time auxiliary draw floors at zero
    assert solar_share_ratio(1.0, 0.0) is None
    assert hour_start(HOUR + timedelta(minutes=37, seconds=5)) == HOUR


@pytest.mark.asyncio
async def test_system_share_uses_actual_solar_over_total_load() -> None:
    window = _FakeWindow({"solar_actual": 20_000.0, "solar_forecast": 25_000.0, "total": 50_000.0}, HOUR)
    share = await solar_share_of_load(SYSTEM_ZONE, HOUR + timedelta(minutes=30), window=window)
    assert share is not None
    assert share.share == pytest.approx(0.4)
    assert share.solar_basis == "actual"
    assert share.hour_start == HOUR


@pytest.mark.asyncio
async def test_system_share_falls_back_to_forecast_solar_when_no_actual() -> None:
    window = _FakeWindow({"solar_forecast": 25_000.0, "total": 50_000.0}, HOUR)
    share = await solar_share_of_load(SYSTEM_ZONE, HOUR, window=window)
    assert share is not None
    assert share.solar_basis == "forecast"
    assert share.share == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_zone_share_sums_mapped_regions() -> None:
    window = _FakeWindow(
        {
            solar_actual_series("FarWest"): 3_000.0,
            solar_actual_series("NorthWest"): 1_000.0,
            "farWest": 5_000.0,
        },
        HOUR,
    )
    share = await solar_share_of_load(
        "farWest", HOUR, zone_regions={"farWest": ("FarWest", "NorthWest")}, window=window
    )
    assert share is not None
    assert share.solar_mw == 4_000.0
    assert share.share == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_share_is_none_when_not_measurable() -> None:
    window = _FakeWindow({"solar_actual": 20_000.0}, HOUR)  # no posted load (e.g. a future hour)
    assert await solar_share_of_load(SYSTEM_ZONE, HOUR, window=window) is None
    # An unmapped weather zone: no guessed region pairing.
    assert await solar_share_of_load("coast", HOUR, window=_FakeWindow({"coast": 1.0}, HOUR)) is None
    # A mapped zone with one region missing: no partial (understated) sum.
    partial = _FakeWindow({solar_actual_series("FarWest"): 3_000.0, "farWest": 5_000.0}, HOUR)
    assert (
        await solar_share_of_load(
            "farWest", HOUR, zone_regions={"farWest": ("FarWest", "NorthWest")}, window=partial
        )
        is None
    )


class _Cfg:
    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def get(self, key: str, default: object = None) -> object:
        return self._values.get(key, default)


def test_zone_regions_from_config() -> None:
    cfg = _Cfg({"feeds.ercot.solar_share_zones": {"farWest": ["FarWest"]}})
    assert zone_regions_from_config(cfg) == {"farWest": ("FarWest",)}
    assert zone_regions_from_config(_Cfg({})) == {}
    with pytest.raises(ValueError, match="unknown solar region"):
        zone_regions_from_config(_Cfg({"feeds.ercot.solar_share_zones": {"west": ["Westish"]}}))
