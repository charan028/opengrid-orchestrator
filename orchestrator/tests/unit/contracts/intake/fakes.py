"""In-memory fakes for `opengrid.contracts.intake`'s ports (BUILD.md "use fakes for siblings")."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class FakeMarketDataPort:
    energy_prices: dict[str, float] = field(default_factory=dict)
    as_mcpc: dict[str, float] = field(default_factory=dict)

    async def latest_energy_price_usd_per_mwh(self, series_key: str) -> float | None:
        return self.energy_prices.get(series_key)

    async def latest_as_mcpc_usd_per_mwh(self, product_code: str) -> float | None:
        return self.as_mcpc.get(product_code)


@dataclass
class FakeForecastScenarios:
    """Callable fake for `intake.ForecastScenariosFn`: returns P50 points at a fixed price for every
    15-min interval in the requested horizon, keyed to one `series_key` -- enough for hand-computed
    energy-arbitrage tests without pulling in `opengrid.forecast`'s real scheduler."""

    series_key: str
    price_by_interval: dict[datetime, float] = field(default_factory=dict)

    async def __call__(self, horizon_start: datetime, horizon_end: datetime) -> list[_Point]:
        return [
            _Point(
                scenario="P50",
                probability=0.5,
                interval_start=t,
                value=v,
                series_key=self.series_key,
                kind="price",
            )
            for t, v in self.price_by_interval.items()
            if horizon_start <= t < horizon_end
        ]


@dataclass(frozen=True, slots=True)
class _Point:
    scenario: str
    probability: float
    interval_start: datetime
    value: float
    series_key: str
    kind: str
