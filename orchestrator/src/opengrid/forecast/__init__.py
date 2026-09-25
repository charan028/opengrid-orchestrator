"""opengrid.forecast -- quantile-persistence library (02b S3). Owner: forecast agent (BUILD.md S4).

Produces P10/P50/P90 quantile scenarios for price ($/MWh, settlement point) and load (MW, weather
zone) over a 24h/15-min horizon, persisted to `og.forecast` and consumed by `selector.run_gate`
(02a S3) as its 3-scenario input. Method: 02b S3 same-slot/day-type quantile pooling over up to 14
days, with a diurnal-profile fallback and NOT_FOR_FIRM flagging when history is short, and 1.3x band
widening plus NOT_FOR_FIRM when the live feed is stale (see `quantiles.py` for the pure math and
`service.py` for the orchestration).

Runs inside whichever process wires it (02b S1.2 places it in `og-feeds`'s cycle for MVP-S; nothing in
this package assumes a particular host process -- `configure()` is called once at process startup with
a `HistoryProvider` (structurally `opengrid.feeds`) and a `ForecastBackend` (`PgForecastBackend`), and
`run_forecast_cycle()`/`scenarios()` use whatever was configured).

Public interface (INTERFACES.md, fixed): `scenarios(horizon_start, horizon_end)`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from opengrid.forecast.backend import ForecastBackend, HistoryProvider
from opengrid.forecast.models import FirmFitness, ForecastKind, ForecastRow, ScenarioPoint
from opengrid.forecast.service import compute_and_persist, rows_to_scenario_points
from opengrid.platform.config import Config

__all__ = [
    "FirmFitness",
    "ForecastBackend",
    "ForecastKind",
    "ForecastRow",
    "HistoryProvider",
    "ScenarioPoint",
    "configure",
    "run_forecast_cycle",
    "scenarios",
]


@dataclass(frozen=True, slots=True)
class _ForecastState:
    cfg: Config
    history: HistoryProvider
    backend: ForecastBackend


_state: _ForecastState | None = None


def configure(cfg: Config, *, history: HistoryProvider, backend: ForecastBackend) -> None:
    """Wire the process-level dependencies once at startup (called from the host process's `main.py`,
    e.g. `og-feeds`'s). Must run before `scenarios()`/`run_forecast_cycle()`."""
    global _state
    _state = _ForecastState(cfg=cfg, history=history, backend=backend)


def _require_state() -> _ForecastState:
    if _state is None:
        raise RuntimeError(
            "opengrid.forecast.configure() must be called before scenarios()/run_forecast_cycle()"
        )
    return _state


async def scenarios(horizon_start: datetime, horizon_end: datetime) -> list[ScenarioPoint]:
    """Return the persisted P10/P50/P90 scenario set (weights 0.25/0.5/0.25, 02a S3.2) for every
    series covering every 15-min interval in `[horizon_start, horizon_end)`. Reads `og.forecast`
    only -- it does not recompute; `run_forecast_cycle()` (on its own cadence) keeps that table
    current."""
    state = _require_state()
    rows = await state.backend.fetch_range(horizon_start, horizon_end)
    return rows_to_scenario_points(rows)


async def run_forecast_cycle(now: datetime | None = None) -> list[ForecastRow]:
    """Recompute and persist the full horizon (02b S3: every 15 minutes, or on demand when `feeds`
    reports a fresh batch). Returns the rows written."""
    state = _require_state()
    return await compute_and_persist(state.cfg, state.history, state.backend, now=now)
