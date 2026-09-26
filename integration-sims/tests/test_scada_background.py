"""Tests for ogsim.scada.background -- defect fix: `substation_load_kw` history is the WHOLE
simulated feeder's aggregate (every bank together), not one bank's share. Before this fix,
`BackgroundLoadModel` replayed that whole-feeder mean onto every bank independently, so each of the
40 banks reported ~3,000 kVA against its 600 kVA rating (40 false `ALR-SCADA-OVERLOAD` alerts on the
live server)."""

from __future__ import annotations

import csv

import numpy as np
import pytest

from ogsim.scada.background import BackgroundLoadModel, load_history_mean_kw

# The live history file's substation_load_kw mean observed on the base server (dispatch-live pass):
# ~3,000 kW aggregate across the whole 40-bank/2,000-hub feeder.
_SUBSTATION_MEAN_KW = 3_000.0
_BANK_COUNT = 40
_BANK_KVA_RATING = 600.0
_ASSUMED_POWER_FACTOR = 0.98


def _write_history_tsv(path, values: list[float]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t")
        writer.writerow(["asset", "signal_type", "unit", "reading_time_utc", "value", "quality"])
        for v in values:
            writer.writerow(["substation-1", "substation_load_kw", "kW", "2026-09-25T00:00:00Z", v, "GOOD"])


def test_load_history_mean_kw_reads_substation_series(tmp_path) -> None:
    path = tmp_path / "history.tsv"
    _write_history_tsv(path, [2_900.0, 3_000.0, 3_100.0])

    assert load_history_mean_kw(str(path)) == pytest.approx(3_000.0)


def test_background_model_divides_whole_feeder_history_by_bank_count(tmp_path) -> None:
    """The defect: `base_kw` must be this bank's ~1/`bank_count` share of the substation-wide
    reading, not the whole feeder's total replayed onto every bank."""
    path = tmp_path / "history.tsv"
    _write_history_tsv(path, [_SUBSTATION_MEAN_KW] * 5)
    rng = np.random.default_rng(0)

    model = BackgroundLoadModel(_BANK_COUNT, base_kw_default=200.0, history_tsv_path=str(path), rng=rng)

    assert model.base_kw == pytest.approx(_SUBSTATION_MEAN_KW / _BANK_COUNT)


def test_background_model_bank_load_stays_under_rating_with_history_seeded(tmp_path) -> None:
    """End-to-end shape check: with the whole-feeder history mean seeded in, a bank's typical
    background load (no battery contribution, no injected anomaly) converts to well under its 600
    kVA rating -- proving the fix, not just the intermediate `base_kw` value."""
    path = tmp_path / "history.tsv"
    _write_history_tsv(path, [_SUBSTATION_MEAN_KW] * 5)
    rng = np.random.default_rng(0)
    model = BackgroundLoadModel(_BANK_COUNT, base_kw_default=200.0, history_tsv_path=str(path), rng=rng)

    noise = np.zeros(_BANK_COUNT)
    load_kw = model.load_kw(epoch_seconds=0.0, noise=noise)
    kva = np.abs(load_kw) / _ASSUMED_POWER_FACTOR

    assert np.all(kva < _BANK_KVA_RATING)


def test_background_model_falls_back_to_default_without_history(tmp_path) -> None:
    missing = tmp_path / "no-history.tsv"
    rng = np.random.default_rng(0)

    model = BackgroundLoadModel(_BANK_COUNT, base_kw_default=200.0, history_tsv_path=str(missing), rng=rng)

    assert model.base_kw == pytest.approx(200.0)
