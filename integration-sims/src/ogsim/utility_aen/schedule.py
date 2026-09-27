"""ogsim.utility_aen.schedule -- pure: the day's weather and the day's peak call (no I/O, no clock).

A real utility calls its toll at its evening peak on hot days. `day_weather` draws a reproducible daily
high per (seed, date); `plan_for_day` turns a hot day into one call inside the configured window
(America/Chicago wall clock, so it keeps its local times across DST), sized `call_kw` for
`duration_min`, never longer than the window or the 90 min product.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from ogsim.utility_aen.config import PRODUCT_CAP_MIN, ScheduleConfig, WeatherConfig

LOCAL_TZ = ZoneInfo("America/Chicago")


@dataclass(frozen=True)
class DayWeather:
    day: date
    high_f: float
    hot: bool


@dataclass(frozen=True)
class PlannedCall:
    """The day's call: signed kW (< 0, discharge), start (UTC), duration."""

    day: date
    call_ref: str
    kw: float
    start: datetime
    duration_min: int

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.duration_min)


def day_weather(day: date, cfg: WeatherConfig) -> DayWeather:
    if cfg.mode == "hot":
        return DayWeather(day, cfg.hot_threshold_f, True)
    if cfg.mode == "mild":
        return DayWeather(day, cfg.hot_threshold_f - 10.0, False)
    rng = random.Random(f"{cfg.seed}:{day.isoformat()}")  # noqa: S311 -- simulation, not crypto
    high = round(rng.uniform(cfg.min_f, cfg.max_f), 1)
    return DayWeather(day, high, high >= cfg.hot_threshold_f)


def local_day(now: datetime) -> date:
    return now.astimezone(LOCAL_TZ).date()


def plan_for_day(
    day: date, weather: DayWeather, cfg: ScheduleConfig, *, utility_id: str
) -> PlannedCall | None:
    """The day's peak call, or None on a day that is not hot (or with the schedule disabled). The start
    is on a 5-minute grid inside the window, chosen reproducibly per date."""
    if not cfg.enabled or not weather.hot:
        return None
    start_local = datetime.combine(day, cfg.window_start, tzinfo=LOCAL_TZ)
    end_local = datetime.combine(day, cfg.window_end, tzinfo=LOCAL_TZ)
    window_min = int((end_local - start_local).total_seconds() // 60)
    duration = min(cfg.duration_min, PRODUCT_CAP_MIN, window_min)
    slack_steps = (window_min - duration) // 5
    offset = random.Random(f"start:{day.isoformat()}").randint(0, slack_steps) * 5  # noqa: S311
    start = (start_local + timedelta(minutes=offset)).astimezone(UTC)
    return PlannedCall(
        day=day,
        call_ref=f"{utility_id.lower()}-peak-{day.isoformat()}",
        kw=-abs(cfg.call_kw),
        start=start,
        duration_min=duration,
    )
