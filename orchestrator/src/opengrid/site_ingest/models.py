"""Wire models for the customer-side closed-loop signals, mirroring `interfaces/mqtt/customer_site_meter
.schema.json` and `interfaces/mqtt/pipeline_corridor_current.schema.json` field for field (both sides
validate against the schemas; `tests/unit/site_ingest` checks these models accept and reject the same
payloads the schemas do). Plus the resolved feedback value the controllers read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field, Strict

Quality = Literal["GOOD", "SUSPECT", "BAD"]


def _iso_text(value: object) -> object:
    """The schemas carry `ts` as an ISO-8601 string; a bare number is not accepted as a timestamp."""
    if not isinstance(value, str | datetime):
        raise ValueError("ts must be an ISO-8601 date-time string")
    return value


# Strict numbers and strings: JSON Schema "number" rejects "1.5" and true, which lax pydantic would coerce.
Number = Annotated[float, Strict()]
NonNegative = Annotated[float, Strict(), Field(ge=0)]
NonEmpty = Annotated[str, Strict(), Field(min_length=1)]
Timestamp = Annotated[AwareDatetime, BeforeValidator(_iso_text)]


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SiteMeterReading(_Wire):
    """DATA_CENTER site meter at the point of common coupling (S4.b); `p_kw` + import, - export."""

    site_id: NonEmpty
    customer_id: NonEmpty
    ts: Timestamp
    p_kw: Number
    q_kvar: Number
    v_rms_a_v: NonNegative
    v_rms_b_v: NonNegative
    v_rms_c_v: NonNegative
    i_rms_a_a: NonNegative
    i_rms_b_a: NonNegative
    i_rms_c_a: NonNegative
    freq_hz: NonNegative
    pf: Annotated[float, Strict(), Field(ge=-1, le=1)]
    thd_v_pct: NonNegative
    thd_i_pct: NonNegative
    quality: Quality


class CorridorCurrentReading(_Wire):
    """PIPELINE_AC corridor signal (S4.a): the AC current induced on the pipe and the customer's limit."""

    corridor_id: NonEmpty
    line_id: NonEmpty
    customer_id: NonEmpty
    ts: Timestamp
    i_ac_a: NonNegative
    limit_a: NonNegative
    quality: Quality


SignalKind = Literal["site_meter", "corridor"]


@dataclass(frozen=True, slots=True)
class FeedbackValue:
    """One resolved `feedback_signal_ref` (S1.3) with its freshness. `usable` is what a controller may
    act on: a GOOD reading no older than the profile's freshness gate; anything else is signal loss."""

    ref: str
    value: float
    ts: datetime
    age_s: float
    quality: Quality
    max_age_s: float

    @property
    def fresh(self) -> bool:
        return self.age_s <= self.max_age_s

    @property
    def usable(self) -> bool:
        return self.fresh and self.quality == "GOOD"
