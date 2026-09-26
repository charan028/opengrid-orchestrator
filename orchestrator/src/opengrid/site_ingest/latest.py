"""The latest reading per site meter / corridor, in memory, and `feedback_signal_ref` resolution (S1.3).

Pure: no I/O, the clock is passed in. Freshness is measured from the reading's own `ts` (the sensor's
time), so a delayed or replayed message is as old as it really is; a reading stamped further in the
future than `max_future_skew_s` is refused, so a skewed publisher clock can never look fresh forever.
An older reading than the one held never replaces it (out-of-order delivery).

`feedback_signal_ref` forms (the profile templates' `[service_profile].feedback_signal_ref`):
`site_meter:<site_id>:<field>` (e.g. `site_meter:site-dc-01:p_kw`) and `corridor:<corridor_id>:<field>`
(e.g. `corridor:corr-07:i_ac_a`). Resolution is scoped to the obligation's customer: a site or corridor
id published by another customer never resolves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast, get_args

from opengrid.core.timeutil import to_utc
from opengrid.site_ingest.models import CorridorCurrentReading, FeedbackValue, SignalKind, SiteMeterReading

Reading = SiteMeterReading | CorridorCurrentReading

#: Numeric fields a ref may name, per signal kind.
SITE_METER_FIELDS = frozenset(
    {
        "p_kw",
        "q_kvar",
        "v_rms_a_v",
        "v_rms_b_v",
        "v_rms_c_v",
        "i_rms_a_a",
        "i_rms_b_a",
        "i_rms_c_a",
        "freq_hz",
        "pf",
        "thd_v_pct",
        "thd_i_pct",
    }
)
CORRIDOR_FIELDS = frozenset({"i_ac_a", "limit_a"})
_FIELDS: dict[str, frozenset[str]] = {"site_meter": SITE_METER_FIELDS, "corridor": CORRIDOR_FIELDS}
_REF_PARTS = 3


class FeedbackRefError(ValueError):
    """A `feedback_signal_ref` that is not one of the two supported forms (a profile/config error)."""


class StaleOrFutureReadingError(ValueError):
    """A reading whose timestamp is too far ahead of the receiver's clock."""


@dataclass(frozen=True, slots=True)
class FeedbackRef:
    kind: SignalKind
    source_id: str
    field: str


def parse_feedback_ref(ref: str) -> FeedbackRef:
    """Parse `<kind>:<source_id>:<field>`; raises `FeedbackRefError` on any other shape."""
    parts = ref.split(":")
    if len(parts) != _REF_PARTS or not all(parts):
        raise FeedbackRefError(f"malformed feedback_signal_ref {ref!r}")
    kind, source_id, field = parts
    if kind not in get_args(SignalKind) or field not in _FIELDS[kind]:
        raise FeedbackRefError(f"unsupported feedback_signal_ref {ref!r}")
    return FeedbackRef(kind=cast(SignalKind, kind), source_id=source_id, field=field)


def source_key(reading: Reading) -> tuple[SignalKind, str, str]:
    """(kind, customer_id, site_id | corridor_id): one latest value per customer and source."""
    if isinstance(reading, SiteMeterReading):
        return ("site_meter", reading.customer_id, reading.site_id)
    return ("corridor", reading.customer_id, reading.corridor_id)


class LatestValues:
    """One latest reading per (kind, customer_id, source_id). Not thread-safe: owned by one event loop."""

    def __init__(self, *, max_future_skew_s: float) -> None:
        self._max_future_skew_s = max_future_skew_s
        self._latest: dict[tuple[SignalKind, str, str], Reading] = {}

    def offer(self, reading: Reading, *, now: datetime) -> bool:
        """Keep `reading` if it is the newest for its source. Returns whether it became the latest.
        Raises `StaleOrFutureReadingError` when `ts` is beyond `now + max_future_skew_s`."""
        if (to_utc(reading.ts) - to_utc(now)).total_seconds() > self._max_future_skew_s:
            raise StaleOrFutureReadingError(
                f"reading ts {reading.ts.isoformat()} is ahead of the receiver clock"
            )
        key = source_key(reading)
        held = self._latest.get(key)
        if held is not None and to_utc(held.ts) >= to_utc(reading.ts):
            return False
        self._latest[key] = reading
        return True

    def latest(self, kind: SignalKind, customer_id: str, source_id: str) -> Reading | None:
        return self._latest.get((kind, customer_id, source_id))

    def resolve(self, ref: str, *, customer_id: str, now: datetime, max_age_s: float) -> FeedbackValue | None:
        """The current value behind `ref` for `customer_id`, with its age, or `None` if nothing has been
        received for that customer's source. Raises `FeedbackRefError` on a malformed ref."""
        parsed = parse_feedback_ref(ref)
        reading = self.latest(parsed.kind, customer_id, parsed.source_id)
        if reading is None:
            return None
        return FeedbackValue(
            ref=ref,
            value=float(getattr(reading, parsed.field)),
            ts=reading.ts,
            age_s=max((to_utc(now) - to_utc(reading.ts)).total_seconds(), 0.0),
            quality=reading.quality,
            max_age_s=max_age_s,
        )

    def clear(self) -> None:
        self._latest.clear()
