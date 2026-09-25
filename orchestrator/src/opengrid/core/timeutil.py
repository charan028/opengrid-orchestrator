"""Time/interval utilities: 15-min interval alignment, UTC<->America/Chicago, epoch/seq freshness, clock
quality, staleness comparison.

Single owner per 02b S12 ("Time/interval utilities"). Every module that touches interval_start/interval_end
or epoch/seq imports this rather than re-deriving the arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

INTERVAL_MINUTES = 15
MARKET_TZ = ZoneInfo("America/Chicago")


def to_utc(dt: datetime) -> datetime:
    """Normalize any aware datetime to UTC. Raises ValueError on naive input."""
    if dt.tzinfo is None:
        raise ValueError("timeutil.to_utc requires a timezone-aware datetime")
    return dt.astimezone(UTC)


def to_market_tz(dt: datetime) -> datetime:
    """Render a UTC (or any aware) instant in the America/Chicago market timezone, for UI display only."""
    return to_utc(dt).astimezone(MARKET_TZ)


def floor_to_interval(dt: datetime, minutes: int = INTERVAL_MINUTES) -> datetime:
    """Floor `dt` to the start of its `minutes`-wide slot (UTC-aligned to the top of the hour)."""
    dt = to_utc(dt)
    discard = timedelta(
        minutes=dt.minute % minutes,
        seconds=dt.second,
        microseconds=dt.microsecond,
    )
    return dt - discard


def interval_bounds(dt: datetime, minutes: int = INTERVAL_MINUTES) -> tuple[datetime, datetime]:
    """Return (interval_start, interval_end) for the slot containing `dt`."""
    start = floor_to_interval(dt, minutes)
    return start, start + timedelta(minutes=minutes)


def interval_index(dt: datetime, horizon_start: datetime, minutes: int = INTERVAL_MINUTES) -> int:
    """0-based index of dt's interval within a horizon starting at horizon_start (both UTC-normalized)."""
    start = floor_to_interval(dt, minutes)
    origin = floor_to_interval(horizon_start, minutes)
    delta = start - origin
    return int(delta.total_seconds() // (minutes * 60))


def is_stale(last_seen: datetime | None, threshold_s: float, *, now: datetime | None = None) -> bool:
    """age = now - last_seen; stale = age > threshold. The one function both `feeds` (feed staleness) and
    `health` (hub staleness) call with their own threshold (02b S12)."""
    if last_seen is None:
        return True
    now = now or datetime.now(UTC)
    age = (to_utc(now) - to_utc(last_seen)).total_seconds()
    return age > threshold_s


@dataclass(frozen=True, slots=True)
class FreshnessCheck:
    ok: bool
    reason: str | None = None


def check_command_freshness(
    *,
    epoch: int,
    seq: int,
    last_accepted_epoch: int,
    last_accepted_seq: int,
    issued_at: datetime,
    expires_at: datetime,
    now: datetime | None = None,
) -> FreshnessCheck:
    """K6: sequence/epoch/lease freshness. A command is accepted only if:
    - epoch >= last_accepted_epoch, and if equal, seq > last_accepted_seq (strictly increasing);
    - epoch > last_accepted_epoch resets the sequence counter (a new epoch always wins on epoch alone);
    - now is within [issued_at, expires_at].
    """
    now = now or datetime.now(UTC)
    now = to_utc(now)
    issued_at = to_utc(issued_at)
    expires_at = to_utc(expires_at)

    if epoch < last_accepted_epoch:
        return FreshnessCheck(False, "STALE_EPOCH")
    if epoch == last_accepted_epoch and seq <= last_accepted_seq:
        return FreshnessCheck(False, "STALE_SEQ")
    if now < issued_at:
        return FreshnessCheck(False, "NOT_YET_VALID")
    if now > expires_at:
        return FreshnessCheck(False, "EXPIRED")
    return FreshnessCheck(True, None)


def clock_offset_ok(offset_ms: float, max_offset_ms: float) -> bool:
    """K12/G-20: guardian refuses to sign when its own NTP offset exceeds the configured limit."""
    return abs(offset_ms) <= max_offset_ms
