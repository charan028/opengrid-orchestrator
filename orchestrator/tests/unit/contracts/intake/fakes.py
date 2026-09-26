"""In-memory fakes for `opengrid.contracts.intake`'s ports (BUILD.md "use fakes for siblings")."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from opengrid.contracts.intake.ports import PriceObservation

#: Timestamp the fake gives an AS price with no explicit one: the intake tests' fixed NOW (always fresh).
DEFAULT_AS_PRICE_TS = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


@dataclass
class FakeMarketDataPort:
    energy_prices: dict[str, float] = field(default_factory=dict)
    as_mcpc: dict[str, float] = field(default_factory=dict)
    as_mcpc_ts: dict[str, datetime] = field(default_factory=dict)

    async def latest_energy_price_usd_per_mwh(self, series_key: str) -> float | None:
        return self.energy_prices.get(series_key)

    async def latest_as_mcpc_usd_per_mwh(self, product_code: str) -> float | None:
        return self.as_mcpc.get(product_code)

    async def latest_as_mcpc(self, product_code: str) -> PriceObservation | None:
        value = self.as_mcpc.get(product_code)
        if value is None:
            return None
        return PriceObservation(value, self.as_mcpc_ts.get(product_code, DEFAULT_AS_PRICE_TS))


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
