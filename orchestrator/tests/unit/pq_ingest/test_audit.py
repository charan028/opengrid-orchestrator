"""opengrid.pq_ingest.audit: S6.5 step 2's background audit job (recompute from raw
samples, compare against the hub's concurrent summary)."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from opengrid.core.models.pq import PqWaveformSummaryRow
from opengrid.pq_ingest.audit import PQ_EDGE_MISCALIBRATION_SUSPECTED, audit_current_channel

SAMPLE_RATE_HZ = 7680.0
N_SAMPLES = 1280  # 10 cycles at 60 Hz


def _pure_sine(freq_hz: float = 60.0, amplitude: float = 10000.0) -> list[int]:
    t = np.arange(N_SAMPLES) / SAMPLE_RATE_HZ
    return [round(v) for v in amplitude * np.sin(2.0 * np.pi * freq_hz * t)]


def _summary(**kwargs: object) -> PqWaveformSummaryRow:
    defaults: dict[str, object] = {"hub_id": "hub-00000", "ts": datetime(2026, 9, 26, tzinfo=UTC)}
    defaults.update(kwargs)
    return PqWaveformSummaryRow(**defaults)


def test_matching_summary_not_suspected() -> None:
    samples = _pure_sine()
    summary = _summary(thd_i_pct_a=0.0, freq_hz=60.0)
    finding = audit_current_channel(samples, SAMPLE_RATE_HZ, summary, "A")
    assert finding.suspected is False
    assert finding.reason is None
    assert finding.recomputed_thd_i_pct == pytest.approx(0.0, abs=0.5)


def test_mismatched_thd_flags_suspected() -> None:
    samples = _pure_sine()  # true THD ~= 0
    summary = _summary(thd_i_pct_a=10.0, freq_hz=60.0)  # hub claims 10% THD
    finding = audit_current_channel(samples, SAMPLE_RATE_HZ, summary, "A")
    assert finding.suspected is True
    assert finding.reason == PQ_EDGE_MISCALIBRATION_SUSPECTED


def test_mismatched_frequency_flags_suspected() -> None:
    samples = _pure_sine()
    summary = _summary(thd_i_pct_a=0.0, freq_hz=61.0)  # 1 Hz off, well past the tolerance
    finding = audit_current_channel(samples, SAMPLE_RATE_HZ, summary, "A")
    assert finding.suspected is True


def test_missing_summary_fields_not_suspected() -> None:
    samples = _pure_sine()
    summary = _summary()  # no thd_i_pct_a / freq_hz at all
    finding = audit_current_channel(samples, SAMPLE_RATE_HZ, summary, "A")
    assert finding.suspected is False
    assert finding.reason is None
