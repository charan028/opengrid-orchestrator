"""Tests for orchestration (`service.py`) and the public `opengrid.forecast` interface, using fakes for
`HistoryProvider`/`ForecastBackend` (BUILD.md task instruction: "use fakes for other modules") in place
of `opengrid.feeds` and Postgres. Covers TS-02-06's "recompute each 15-min gate" via the 96-step shape
check, plus the low-history (2.5-day `HIST` import) and stale-feed degrade paths end to end.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import opengrid.forecast as forecast
from opengrid.core.models.platform import FeedObs
from opengrid.forecast.models import ForecastRow
from opengrid.forecast.service import (
    DEFAULT_PRICE_SERIES,
    DEFAULT_WEATHER_ZONES_BY_LOAD_ZONE,
    compute_and_persist,
    rows_to_scenario_points,
)
from opengrid.forecast.solar import clear_sky_shape
from opengrid.platform.config import Config

_NOW = datetime(2026, 7, 20, 12, 3, tzinfo=UTC)  # a Monday, mid-slot -- exercises floor_to_interval


class FakeHistory:
    """Structurally satisfies `HistoryProvider` (same shape as `opengrid.feeds`)."""

    def __init__(self, observations: dict[str, list[FeedObs]], *, quality: str = "GOOD") -> None:
        self._observations = observations
        self._quality = quality

    async def latest(self, series: str) -> FeedObs:
        obs = self._observations.get(series)
        if not obs:
            raise LookupError(series)
        latest = max(obs, key=lambda o: o.ts)
        return latest.model_copy(update={"quality": self._quality})

    async def window(self, series: str, t0: datetime, t1: datetime) -> list[FeedObs]:
        return [o for o in self._observations.get(series, []) if t0 <= o.ts < t1]


class FakeBackend:
    """Structurally satisfies `ForecastBackend`, storing rows in memory."""

    def __init__(self) -> None:
        self.rows: list[ForecastRow] = []

    async def upsert_rows(self, rows: list[ForecastRow]) -> None:
        keys = {(r.series_key, r.kind, r.interval_start_utc) for r in rows}
        self.rows = [r for r in self.rows if (r.series_key, r.kind, r.interval_start_utc) not in keys]
        self.rows.extend(rows)

    async def fetch_range(self, horizon_start: datetime, horizon_end: datetime) -> list[ForecastRow]:
        return [r for r in self.rows if horizon_start <= r.interval_start_utc < horizon_end]


def _obs(series: str, ts: datetime, value: float, *, quality: str = "GOOD") -> FeedObs:
    return FeedObs(
        source="ERCOT",
        product="np6-905-cd",
        series=series,
        ts=ts,
        value=value,
        unit="usd_per_mwh",
        quality=quality,
        recorded_at=ts,
    )


def _rich_history(series: str, target: datetime) -> list[FeedObs]:
    """~2 weeks of daily observations at every 15-min slot, ample for the direct-sample path."""
    return [
        _obs(series, target - timedelta(days=d, minutes=15 * step), 40.0 + step + d)
        for d in range(14)
        for step in range(96)
    ]


def _short_history(series: str, target: datetime) -> list[FeedObs]:
    """2.5 days of observations, per the feeds agent's `HIST` import -- the short-history case."""
    return [
        _obs(series, target - timedelta(hours=h * 0.25), 50.0 + (h % 10)) for h in range(int(2.5 * 24 * 4))
    ]


def _cfg(**forecast_overrides: object) -> Config:
    data = {
        "forecast": {
            "horizon_hours": 24,
            "resolution_min": 15,
            "price_series": ["HB_TEST"],
            "load_series": ["LZ_TEST"],
            **forecast_overrides,
        }
    }
    return Config(data)


@pytest.mark.asyncio
async def test_compute_and_persist_produces_96_steps_per_series():
    horizon_start = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    history = FakeHistory(
        {
            "HB_TEST": _rich_history("HB_TEST", horizon_start),
            "LZ_TEST": _rich_history("LZ_TEST", horizon_start),
        }
    )
    backend = FakeBackend()
    rows = await compute_and_persist(_cfg(), history, backend, now=_NOW)

    by_series: dict[str, list[ForecastRow]] = {}
    for row in rows:
        by_series.setdefault(row.series_key, []).append(row)

    assert set(by_series) == {"HB_TEST", "LZ_TEST"}
    for series_rows in by_series.values():
        assert len(series_rows) == 96
        assert sorted(r.horizon_step for r in series_rows) == list(range(96))
        for row in series_rows:
            assert row.p10 <= row.p50 <= row.p90
    assert backend.rows == rows


@pytest.mark.asyncio
async def test_compute_and_persist_flags_not_for_firm_on_short_history():
    horizon_start = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    history = FakeHistory({"HB_TEST": _short_history("HB_TEST", horizon_start)}, quality="GOOD")
    backend = FakeBackend()
    cfg = _cfg(load_series=[], pool_day_types_when_short=False)
    rows = await compute_and_persist(cfg, history, backend, now=_NOW)

    assert len(rows) == 96
    assert all(r.firm_fitness == "NOT_FOR_FIRM" for r in rows)
    assert all(r.p10 <= r.p50 <= r.p90 for r in rows)


@pytest.mark.asyncio
async def test_compute_and_persist_pools_day_types_on_short_history(caplog: pytest.LogCaptureFixture):
    """Short history, pooling on (the default): slots with >= 3 samples across Sat/Sun/Mon at that
    time-of-day are stored FIRM_POOLED (and logged as such); slots below that stay NOT_FOR_FIRM."""
    horizon_start = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    history = FakeHistory({"HB_TEST": _short_history("HB_TEST", horizon_start)}, quality="GOOD")
    with caplog.at_level("WARNING", logger="opengrid.forecast.service"):
        rows = await compute_and_persist(_cfg(load_series=[]), history, FakeBackend(), now=_NOW)

    firm = [r for r in rows if r.firm_fitness == "FIRM_POOLED"]
    assert firm and len(firm) < len(rows)
    assert not any(r.firm_fitness == "FIRM_OK" for r in rows)  # no slot meets the strict rule here
    pooled_logs = [r for r in caplog.records if getattr(r, "reason_code", None) == "FIRM_POOLED"]
    assert len(pooled_logs) == 1
    assert pooled_logs[0].pooled_slots == len(firm)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_compute_and_persist_min_samples_firm_is_config_driven():
    horizon_start = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    history = FakeHistory({"HB_TEST": _short_history("HB_TEST", horizon_start)}, quality="GOOD")
    rows = await compute_and_persist(
        _cfg(load_series=[], min_samples_firm=4), history, FakeBackend(), now=_NOW
    )
    assert all(r.firm_fitness == "NOT_FOR_FIRM" for r in rows)  # only 3 days exist: never 4 samples


@pytest.mark.asyncio
async def test_pooled_max_rel_spread_must_be_numeric():
    with pytest.raises(ValueError, match="pooled_max_rel_spread"):
        await compute_and_persist(
            _cfg(load_series=[], pooled_max_rel_spread="wide"), FakeHistory({}), FakeBackend(), now=_NOW
        )


@pytest.mark.asyncio
async def test_compute_and_persist_widens_band_when_series_stale():
    horizon_start = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    fresh_history = FakeHistory({"HB_TEST": _rich_history("HB_TEST", horizon_start)}, quality="GOOD")
    stale_history = FakeHistory({"HB_TEST": _rich_history("HB_TEST", horizon_start)}, quality="STALE")
    fresh_rows = await compute_and_persist(_cfg(load_series=[]), fresh_history, FakeBackend(), now=_NOW)
    stale_rows = await compute_and_persist(_cfg(load_series=[]), stale_history, FakeBackend(), now=_NOW)

    fresh_by_step = {r.horizon_step: r for r in fresh_rows}
    for stale_row in stale_rows:
        fresh_row = fresh_by_step[stale_row.horizon_step]
        assert stale_row.firm_fitness == "NOT_FOR_FIRM"
        assert (stale_row.p90 - stale_row.p10) > (fresh_row.p90 - fresh_row.p10)


@pytest.mark.asyncio
async def test_load_zone_load_is_summed_from_its_weather_zones():
    """Bug regression: `np6-345-cd` (`feeds.normalize.ercot_load_to_feed_obs`) posts actual system
    load under ERCOT *weather*-zone series names (`north`, `northC`, ...), never under a load-zone key
    like `LZ_NORTH` -- so defaulting `load_series` to `[fleet].zones` and reading history directly
    under that key silently found zero data every cycle. `DEFAULT_WEATHER_ZONES_BY_LOAD_ZONE` must map
    `LZ_NORTH` to its weather zones (`north`, `northC`) and sum them into the `LZ_NORTH` forecast."""
    horizon_start = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    # One sample per weather zone, both at the same instant, far enough back that the same-slot pool
    # (needs >= 3 samples) never fills -- the diurnal fallback fires deterministically for every step,
    # and with a single summed timestamp its p10/p50/p90 all equal the summed value exactly.
    history = FakeHistory(
        {
            "north": [_obs("north", horizon_start - timedelta(days=1), 100.0)],
            "northC": [_obs("northC", horizon_start - timedelta(days=1), 50.0)],
        }
    )
    backend = FakeBackend()
    cfg = _cfg(price_series=[], load_series=["LZ_NORTH"])

    rows = await compute_and_persist(cfg, history, backend, now=_NOW)

    assert len(rows) == 96
    assert all(r.series_key == "LZ_NORTH" and r.kind == "load" for r in rows)
    assert all(r.p50 == pytest.approx(150.0) for r in rows)  # 100 (north) + 50 (northC)


@pytest.mark.asyncio
async def test_austin_load_zone_sums_its_approximate_weather_zone():
    """Build phase, 2026-09-26 (market-model-two-markets.md): `LZ_AEN` (Austin Energy) has no ERCOT
    weather-zone definition of its own, so `DEFAULT_WEATHER_ZONES_BY_LOAD_ZONE` maps it to the
    approximate `southC` weather zone (documented as approximate, pending an authoritative mapping).
    Once an operator adds "LZ_AEN" to `[fleet].zones`/`load_series`, it must compute like any other
    load zone -- summed from its mapped weather zone(s), never silently empty."""
    horizon_start = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    history = FakeHistory({"southC": [_obs("southC", horizon_start - timedelta(days=1), 42.0)]})
    backend = FakeBackend()
    cfg = _cfg(price_series=[], load_series=["LZ_AEN"])

    rows = await compute_and_persist(cfg, history, backend, now=_NOW)

    assert len(rows) == 96
    assert all(r.series_key == "LZ_AEN" and r.kind == "load" for r in rows)
    assert all(r.p50 == pytest.approx(42.0) for r in rows)


@pytest.mark.asyncio
async def test_load_zone_without_weather_mapping_logs_and_falls_back(caplog: pytest.LogCaptureFixture):
    """An unmapped load zone falls back to reading its own code directly (the pre-fix behaviour) but
    must say so loudly (BUILD.md S5a "no silent fallbacks"), not just silently produce zero rows."""
    horizon_start = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    history = FakeHistory({"LZ_UNMAPPED": _rich_history("LZ_UNMAPPED", horizon_start)})
    backend = FakeBackend()
    cfg = _cfg(price_series=[], load_series=["LZ_UNMAPPED"])

    with caplog.at_level("WARNING"):
        rows = await compute_and_persist(cfg, history, backend, now=_NOW)

    assert len(rows) == 96  # still computed, via the direct-key fallback
    assert any("weather-zone mapping" in record.message for record in caplog.records)


def test_default_price_series_includes_the_four_market_only_zones() -> None:
    """D-24 build task: LZ_AEN/LZ_CPS/LZ_LCRA/LZ_RAYBN forecast price even though OpenGrid has no
    dispatchable hubs in them yet (market intelligence only, no dispatch impact)."""
    for zone in ("LZ_AEN", "LZ_CPS", "LZ_LCRA", "LZ_RAYBN"):
        assert zone in DEFAULT_PRICE_SERIES


def test_lcra_and_raybn_have_a_documented_weather_zone_mapping() -> None:
    assert DEFAULT_WEATHER_ZONES_BY_LOAD_ZONE["LZ_LCRA"]
    assert DEFAULT_WEATHER_ZONES_BY_LOAD_ZONE["LZ_RAYBN"]


@pytest.mark.asyncio
async def test_solar_shape_pulls_midday_price_down_and_lifts_evening_ramp():
    """D-24: with an ERCOT solar-forecast signal available, the midday price slot must come out lower,
    and the post-sunset evening-ramp slot higher, than the plain quantile-persistence baseline computed
    from the exact same price history with no solar signal at all."""
    horizon_start = datetime(2026, 7, 20, 5, 0, tzinfo=UTC)  # 00:00 local (America/Chicago, UTC-5 in July)
    price_history = _rich_history("HB_TEST", horizon_start)

    # A diurnal ERCOT solar-forecast series covering the whole horizon: peaks at local solar noon,
    # zero at night -- exactly the `forecast.solar.clear_sky_shape` curve, scaled to "MW".
    solar_obs = [
        _obs(
            "solar_forecast",
            horizon_start + timedelta(minutes=15 * step),
            1000.0 * clear_sky_shape(horizon_start + timedelta(minutes=15 * step)),
        )
        for step in range(96)
    ]

    baseline_rows = await compute_and_persist(
        _cfg(price_series=["HB_TEST"], load_series=[]),
        FakeHistory({"HB_TEST": price_history}),
        FakeBackend(),
        now=horizon_start,
    )
    shaped_rows = await compute_and_persist(
        _cfg(price_series=["HB_TEST"], load_series=[]),
        FakeHistory({"HB_TEST": price_history, "solar_forecast": solar_obs}),
        FakeBackend(),
        now=horizon_start,
    )

    baseline_by_step = {r.horizon_step: r for r in baseline_rows}
    shaped_by_step = {r.horizon_step: r for r in shaped_rows}

    midday_step = 48  # horizon_start (00:00 local) + 12h = solar noon
    evening_ramp_step = 78  # horizon_start + 19h30m local = just past SUNSET_HOUR

    assert shaped_by_step[midday_step].p50 < baseline_by_step[midday_step].p50
    assert shaped_by_step[evening_ramp_step].p50 > baseline_by_step[evening_ramp_step].p50
    assert all(r.p10 <= r.p50 <= r.p90 for r in shaped_rows)


@pytest.mark.asyncio
async def test_compute_and_persist_skips_series_with_no_history_at_all():
    history = FakeHistory({})
    backend = FakeBackend()
    rows = await compute_and_persist(_cfg(load_series=[]), history, backend, now=_NOW)
    assert rows == []


def test_rows_to_scenario_points_expands_three_weighted_points():
    row = ForecastRow(
        series_key="HB_TEST",
        kind="price",
        interval_start_utc=_NOW,
        horizon_step=0,
        p10=10.0,
        p50=20.0,
        p90=30.0,
    )
    points = rows_to_scenario_points([row])
    assert len(points) == 3
    by_scenario = {p.scenario: p for p in points}
    assert by_scenario["P10"].value == 10.0
    assert by_scenario["P50"].value == 20.0
    assert by_scenario["P90"].value == 30.0
    assert sum(p.probability for p in points) == pytest.approx(1.0)
    assert all(p.series_key == "HB_TEST" and p.kind == "price" for p in points)


@pytest.mark.asyncio
async def test_public_interface_requires_configure_first():
    import opengrid.forecast as fresh_forecast_module

    fresh_forecast_module._state = None
    with pytest.raises(RuntimeError, match="configure"):
        await fresh_forecast_module.scenarios(_NOW, _NOW + timedelta(hours=1))


@pytest.mark.asyncio
async def test_public_interface_end_to_end_via_configure():
    horizon_start = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    history = FakeHistory({"HB_TEST": _rich_history("HB_TEST", horizon_start)})
    backend = FakeBackend()
    forecast.configure(_cfg(load_series=[]), history=history, backend=backend)
    try:
        await forecast.run_forecast_cycle(now=_NOW)
        points = await forecast.scenarios(horizon_start, horizon_start + timedelta(hours=1))
        # 4 x 15-min steps in one hour, 3 scenarios each.
        assert len(points) == 12
        assert all(p.scenario in ("P10", "P50", "P90") for p in points)
    finally:
        forecast._state = None
