"""ogsim.scada.background -- feeder background load model (02b §5.1).

Tries to seed a background-load shape from the historical
`substation_load_kw` series at `history_tsv_path` (server-only); falls
back to a synthetic diurnal curve when that file is absent, malformed, or
unreadable -- never crashes on a missing path (BUILD.md explicit ask).
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

SECONDS_PER_DAY = 86400.0
DIURNAL_PEAK_HOUR = 18.0


def load_history_mean_kw(path: str) -> float | None:
    """Reads `substation_load_kw` readings from the history TSV and returns
    their mean, or None if the file/column is unavailable for any reason."""
    file_path = Path(path)
    try:
        with file_path.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh, delimiter="\t")
            values = [
                float(row["value"])
                for row in reader
                if row.get("signal_type") == "substation_load_kw" and row.get("value")
            ]
    except (FileNotFoundError, OSError, csv.Error, KeyError, ValueError) as exc:
        logger.info("scada background: history file unavailable (%s), using synthetic load", exc)
        return None
    if not values:
        return None
    return float(np.mean(values))


def synthetic_diurnal_kw(hour_of_day: np.ndarray, base_kw: float) -> np.ndarray:
    """A smooth diurnal feeder-load curve: ~55% of `base_kw` overnight,
    rising to `base_kw` around the evening peak."""
    phase = (hour_of_day - DIURNAL_PEAK_HOUR) / 24.0 * 2.0 * np.pi
    bump = (np.cos(phase) + 1.0) / 2.0
    return base_kw * (0.55 + 0.45 * bump)


class BackgroundLoadModel:
    """Per-bank background load: a base level (from history, else config
    default) times a per-bank multiplier, shaped by the diurnal curve."""

    def __init__(
        self, bank_count: int, base_kw_default: float, history_tsv_path: str, rng: np.random.Generator
    ):
        history_mean = load_history_mean_kw(history_tsv_path)
        self.base_kw = history_mean if history_mean is not None else base_kw_default
        self.per_bank_multiplier = rng.uniform(0.8, 1.2, size=bank_count)

    def load_kw(self, epoch_seconds: float, noise: np.ndarray) -> np.ndarray:
        hour_of_day = np.mod(epoch_seconds, SECONDS_PER_DAY) / 3600.0
        base = synthetic_diurnal_kw(np.full(self.per_bank_multiplier.shape, hour_of_day), self.base_kw)
        return base * self.per_bank_multiplier + noise
