"""Row shape for the forecast-owned table `og.forecast` (02b S3).

Owned here (not in `opengrid.core.models`) because `forecast` is the sole owner/writer of this table
(BUILD.md S4); nothing else needs the row shape at import time.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict

ForecastKind = Literal["price", "load"]
#: `FIRM_POOLED` (migration 0040) is firm, admitted by the short-history day-type pooling relaxation
#: (`quantiles.compute_slot_quantiles`); readers must treat it as firm, like `FIRM_OK`.
FirmFitness = Literal["FIRM_OK", "FIRM_POOLED", "NOT_FOR_FIRM"]
FIRM_VALUES: frozenset[str] = frozenset({"FIRM_OK", "FIRM_POOLED"})


class ForecastRow(BaseModel):
    """Mirrors `og.forecast` (02b S3) exactly: one quantile triple per series/kind/interval."""

    model_config = ConfigDict(extra="forbid")

    series_key: str
    kind: ForecastKind
    interval_start_utc: datetime
    horizon_step: int  # 0..95
    p10: float
    p50: float
    p90: float
    firm_fitness: FirmFitness = "FIRM_OK"
    computed_at: datetime | None = None  # server default `now()` when left unset


class ScenarioPoint(NamedTuple):
    """02a S3 selector input: one weighted scenario value at one interval, for one series.

    `series_key`/`kind` extend the original stub's fields (INTERFACES.md fixes only the
    `scenarios(horizon_start, horizon_end)` call signature, not this tuple's shape) -- without them the
    selector has no way to tell which hub/zone a point belongs to across a multi-series horizon.
    """

    scenario: Literal["P10", "P50", "P90"]
    probability: float
    interval_start: datetime
    value: float
    series_key: str
    kind: ForecastKind
