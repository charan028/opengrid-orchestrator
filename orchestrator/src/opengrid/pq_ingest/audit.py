"""Background audit job (07-delivery/06 S6.5 step 2): periodically re-derives RMS/THD/
frequency from a raw waveform capture's samples using the SAME `opengrid.core.pq.analyze_waveform`
an edge device conceptually runs, and compares it against that hub's concurrent summary.
A disagreement beyond tolerance flags the hub's own edge computation as suspect
(`PQ_EDGE_MISCALIBRATION_SUSPECTED`) -- evidence feeding S5.5's drift detection, not a
replacement for it (S6.5: "the measured pipeline trusts each hub's own edge FFT/THD
computation ... this consumer-side re-check exists specifically because a mis-calibrated
edge computation would otherwise look like measured truth").
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from opengrid.core.models.pq import PqWaveformSummaryRow
from opengrid.core.pq import analyze_waveform

PQ_EDGE_MISCALIBRATION_SUSPECTED = "PQ_EDGE_MISCALIBRATION_SUSPECTED"

# S6.5 gives no numeric tolerance; these mirror the drift-detection floors already named
# in opengrid.core.pq.constants (DRIFT_FLOOR_THD_PCT / a comparable frequency floor) so
# the audit job's "materially different" threshold matches the rest of S5/S6's PQ math
# rather than inventing an unrelated number.
DEFAULT_THD_TOLERANCE_PCT = 1.5
DEFAULT_FREQ_TOLERANCE_HZ = 0.05


@dataclass(frozen=True, slots=True)
class AuditFinding:
    hub_id: str
    suspected: bool
    recomputed_thd_i_pct: float
    recomputed_freq_hz: float
    reason: str | None


def audit_current_channel(
    samples: list[int],
    sample_rate_hz: float,
    concurrent_summary: PqWaveformSummaryRow,
    phase: str,
    *,
    thd_tolerance_pct: float = DEFAULT_THD_TOLERANCE_PCT,
    freq_tolerance_hz: float = DEFAULT_FREQ_TOLERANCE_HZ,
) -> AuditFinding:
    """Recomputes THD_I/frequency from one raw current-channel capture and compares
    against the hub's `thd_i_pct_<phase>`/`freq_hz` at the same instant (S6.5 step 2). A
    summary with no reading for either quantity cannot be audited against and is reported
    NOT suspected (nothing to disagree with) rather than raising."""
    array = np.asarray(samples, dtype=np.float64)
    analysis = analyze_waveform(array, sample_rate_hz)
    summary_thd = getattr(concurrent_summary, f"thd_i_pct_{phase.lower()}")
    summary_freq = concurrent_summary.freq_hz
    if summary_thd is None or summary_freq is None:
        return AuditFinding(concurrent_summary.hub_id, False, analysis.thd_pct, analysis.fundamental_hz, None)

    thd_gap = abs(analysis.thd_pct - float(summary_thd))
    freq_gap = abs(analysis.fundamental_hz - float(summary_freq))
    suspected = thd_gap > thd_tolerance_pct or freq_gap > freq_tolerance_hz
    reason = PQ_EDGE_MISCALIBRATION_SUSPECTED if suspected else None
    return AuditFinding(
        concurrent_summary.hub_id, suspected, analysis.thd_pct, analysis.fundamental_hz, reason
    )
