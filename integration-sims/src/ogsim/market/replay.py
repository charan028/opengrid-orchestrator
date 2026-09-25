"""Replay (a) data mode: 2.5 days of history from
/var/lib/opengrid/import/mariadb_history_signals.tsv, looped with time
shifted to now.

TSV columns (per BUILD.md §5): asset, signal_type, unit, reading_time_utc,
value, quality. We use:
  wholesale_price_mwh   -> SPP hub prices (same series, small per-hub offset)
  substation_load_kw    -> zone load (kW -> MW, distributed across weather zones)

The file is optional: if it is missing (e.g. local dev off the server),
`HistoryReplay.available` is False and callers should fall back to synthetic
mode.
"""

from __future__ import annotations

import bisect
import csv
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path


def _parse_ts(raw: str) -> datetime:
    raw = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.replace(tzinfo=UTC)
        except ValueError:
            continue
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)


@dataclass
class _Series:
    times: list[float] = field(default_factory=list)  # epoch seconds, sorted
    values: list[float] = field(default_factory=list)

    def add(self, t: float, v: float) -> None:
        self.times.append(t)
        self.values.append(v)

    def finalize(self) -> None:
        order = sorted(range(len(self.times)), key=lambda i: self.times[i])
        self.times = [self.times[i] for i in order]
        self.values = [self.values[i] for i in order]

    def value_at(self, t: float) -> float | None:
        if not self.times:
            return None
        idx = bisect.bisect_left(self.times, t)
        if idx == 0:
            return self.values[0]
        if idx >= len(self.times):
            return self.values[-1]
        # nearest neighbour between idx-1 and idx
        before_t, after_t = self.times[idx - 1], self.times[idx]
        if (t - before_t) <= (after_t - t):
            return self.values[idx - 1]
        return self.values[idx]


class HistoryReplay:
    def __init__(self, path: str):
        self.path = Path(path)
        self.available = False
        self._series: dict[str, _Series] = {}
        self._t0: float | None = None
        self._span_s: float | None = None
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", newline="", encoding="utf-8") as f:
                reader = csv.reader(f, delimiter="\t")
                header = next(reader, None)
                if not header:
                    return
                cols = {name.strip().lower(): i for i, name in enumerate(header)}
                required = {"signal_type", "reading_time_utc", "value"}
                if not required.issubset(cols):
                    return
                for row in reader:
                    if len(row) <= max(cols.values()):
                        continue
                    signal_type = row[cols["signal_type"]].strip()
                    try:
                        ts = _parse_ts(row[cols["reading_time_utc"]]).timestamp()
                        value = float(row[cols["value"]])
                    except (ValueError, IndexError):
                        continue
                    self._series.setdefault(signal_type, _Series()).add(ts, value)
        except OSError:
            return
        if not self._series:
            return
        all_times = [t for s in self._series.values() for t in s.times]
        if not all_times:
            return
        for s in self._series.values():
            s.finalize()
        self._t0 = min(all_times)
        t1 = max(all_times)
        self._span_s = max(1.0, t1 - self._t0)
        self.available = True

    def virtual_time(self, real_now: datetime) -> float:
        """Maps a real wall-clock timestamp into the looped history window."""
        assert self._t0 is not None and self._span_s is not None
        elapsed = real_now.timestamp() % self._span_s
        return self._t0 + elapsed

    def value_at(self, signal_type: str, real_now: datetime) -> float | None:
        series = self._series.get(signal_type)
        if series is None:
            return None
        return series.value_at(self.virtual_time(real_now))
