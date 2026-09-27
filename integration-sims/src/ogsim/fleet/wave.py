"""ogsim.fleet.wave -- WP-H: per-inverter waveform summary/raw generation and
publication over the SCADA network (06-service-profiles-and-power-quality.md
S6.4, S7.4, S9.2 Agent H).

Builds the two payload kinds a real hub inverter would emit, from
`ogsim.fleet.pq.InverterSnapshot` (the clean accessor `ogsim.fleet.pq.inverter_state()`
already exposes -- this module never touches `InverterPqState` itself, per WP-H's
brief: "call ogsim.fleet.pq's access function and don't edit it"):

- a periodic PQ **summary**, split into a "fast" sub-block (RMS/freq/PF/aggregate
  THD, published every telemetry tick) and a "harmonic-detail" sub-block (orders
  2-50 mag/angle, published on a slower cadence or on-change) per S6.4b's
  bandwidth mitigation -- this is what keeps a 2,000-10,000 hub fleet's summary
  channel within a few Mbps instead of ~8 Mbps of always-on harmonic detail;
- a **raw waveform capture**, synthesized on demand from the sinusoid-plus-
  harmonics model of S7.4, published only when triggered: an inbound
  `waveform_capture_request` (S6.4b), or this module's own rotating audit
  sample (S6.4b trigger policy item iii, bounded to ~1% of the fleet/minute).

Anomaly effects (frequency drift, harmonic injection, calibration drift, ...)
are already folded into the `InverterSnapshot` this module receives -- it only
synthesizes the wire payload from already-anomalous state, never re-derives PQ
state itself (BUILD.md S1: one owner per function).

Summary physics (WP-K part 1, #16 follow-ups). Per leg, the summary combines every unit
wired to that leg as phasors: the fundamental `i_rms_<leg>` is |sum I_u e^(j phi_u)| (phi_u =
the unit's `phase_angle_error_deg`, which also gives `pf_<leg>`/`phase_angle_deg_<leg>`), and
each harmonic order is the vector sum sum I_u (m_u,k/100) e^(j theta_u,k) (S3.2b). THD_I is the
RSS of those harmonic phasors over the leg's fundamental. The hub-level `harmonics_i` block is
the vector sum over all of the hub's units, expressed as % of the hub's summed per-leg
fundamental, so a consumer that multiplies `mag_pct` by sum(i_rms_<leg>) recovers each
unit's harmonic phasor sum exactly (dual-unit homes included).

Voltage distortion is modelled independently of THD_I through a documented source
(service-drop plus distribution-transformer) impedance, purely inductive at harmonic
frequencies: V_k = j k X_source I_k, with X_source = V_nom / (SCR x I_rated) for a stated
short-circuit ratio SCR against one unit's rated current (`WaveConfig.source_short_circuit_ratio`,
`WaveConfig.unit_rated_kw`). So `harmonics_v` has order-k angle theta_k + 90 deg and a magnitude
that grows with order and with dispatched current, and THD_V is the RSS of V_k over the leg's
`v_rms`. Simplifications (sim only): background grid voltage distortion and neighbouring hubs'
harmonic currents through the shared transformer are not modelled.

S6.4a scaled-int16 harmonic encoding: DEFERRED. The summary still carries harmonics as the
schema's JSON objects (float `mag_pct`/`angle_deg`). Reasons: (1) the bandwidth win S6.4a is
after needs the binary/CBOR summary encoding, not int16 values inside JSON; the S6.4b
harmonic-detail cadence split (`HarmonicDetailScheduler`) already carries the main reduction;
(2) `opengrid.pq_ingest._row_fields` drops fields it does not know, so a packed field sent
before the orchestrator decodes it would silently lose every hub's harmonics (BUILD.md S5a
"no silent fallbacks"). Wiring when it is picked up: add an optional `harmonics_packed`
property to `interfaces/mqtt/pq_waveform_summary.schema.json` (`orders` int array plus
`v_mag`/`v_ang`/`i_mag`/`i_ang` int16 arrays; magnitude LSB 0.01 %, angle LSB 0.1 deg --
additive, so old publishers stay valid); make `pq_ingest.ingest_summary` expand it into
`harmonics_v`/`harmonics_i` before `PqWaveformSummaryRow.model_validate`, accepting either
shape during the transition; only then emit it here behind a `wave:` config switch.

Pure functions plus two small stateful schedulers (`HarmonicDetailScheduler`,
`RotatingAuditSampler`) that hold only cadence/rate-limit bookkeeping. No MQTT
here -- `ogsim.fleet.runtime`/`ogsim.fleet.__main__` own the wire glue, exactly
like `ogsim.fleet.calibration`.
"""

from __future__ import annotations

import cmath
import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np

from ogsim.fleet.commands import parse_rfc3339
from ogsim.fleet.pq import InverterSnapshot

# S6.4(a) raw capture parameters (must match interfaces/mqtt/pq_waveform_raw.schema.json's
# `const` fields exactly -- a real hub advertises a fixed sample rate/window).
SAMPLE_RATE_HZ = 7680.0
CAPTURE_CYCLES = 10
NOMINAL_FREQ_HZ = 60.0
SAMPLES_PER_CAPTURE = int(CAPTURE_CYCLES * SAMPLE_RATE_HZ / NOMINAL_FREQ_HZ)  # 1280

# Split-phase US residential leg-to-neutral nominal (S2's ANSI C84.1 Range A default is
# expressed as +/-% of this). Used only to synthesize a physically-plausible waveform
# amplitude in the simulator; never a live-system constant.
NOMINAL_VOLTAGE_V = 240.0

# A hub with ~zero commanded discharge still has a nonzero synthesized current so its
# waveform capture is not degenerate (FFT analysis needs a nonzero fundamental) -- models
# residual/ripple current, not a real idle-current spec.
IDLE_CURRENT_A_FLOOR = 1.0

_FULL_CIRCLE_DEG = 360.0

_PHASE_LETTERS = ("A", "B", "C")


_CANONICAL_PAIRS: dict[frozenset[str], str] = {
    frozenset({"A", "B"}): "AB",
    frozenset({"B", "C"}): "BC",
    frozenset({"A", "C"}): "CA",
}


def hub_phase_connection(unit_legs: list[str]) -> str:
    """Combines a hub's unit(s) individual single-letter legs into one
    `phase_connection` value from the schema's own enum (`A|B|C|AB|BC|CA|ABC`) --
    a dual-unit home's two independently-assigned legs (S3.1) must still produce a
    schema-conformant hub-level value, in canonical `AB`/`BC`/`CA` order regardless
    of which unit was seeded on which leg first."""
    legs = set(unit_legs)
    if len(legs) == 1:
        return next(iter(legs))
    if len(legs) == 3:
        return "ABC"
    canonical = _CANONICAL_PAIRS.get(frozenset(legs))
    if canonical is None:
        raise ValueError(f"unrecognized phase legs: {unit_legs!r}")
    return canonical


def channels_for_phase_connection(phase_connection: str) -> list[str]:
    """S6.4(a): 2 channels (V, I) for a 1P hub, 4 (V_A,V_B,I_A,I_B) for SPLIT_PHASE,
    6 (V_A,V_B,V_C,I_A,I_B,I_C) for 3P, keyed off `phase_connection`'s length (single
    leg, a leg pair, or "ABC")."""
    if len(phase_connection) == 1:
        return ["V", "I"]
    legs = list(phase_connection) if phase_connection != "ABC" else list(_PHASE_LETTERS)
    return [f"V_{leg}" for leg in legs] + [f"I_{leg}" for leg in legs]


def _leg_suffixes(phase_connection: str) -> list[str]:
    """Single-letter phase code(s) this hub is wired to, e.g. "AB" -> ["A", "B"],
    "ABC" -> ["A", "B", "C"], "B" -> ["B"]."""
    if phase_connection == "ABC":
        return list(_PHASE_LETTERS)
    return list(phase_connection)


@dataclass(frozen=True, slots=True)
class WaveConfig:
    """S6.4b's bandwidth-mitigation cadence and S6.4b trigger-policy-item-iii audit rate,
    loaded from `fleet.yaml`'s `wave:` block (see `ogsim.common.config`).

    `summary_interval_s`/`summary_delta_pct` are the S9 wave-2 fix (build report: the PQ
    summary's "fast" sub-block used to publish on every telemetry tick regardless of this
    config -- tied 1:1 to `FleetConfig.telemetry_interval_s`, 2 s by default -- which is
    exactly the ~1,000 msg/s at 2,000 hubs that stalled `opengrid.pq_ingest`'s one-insert-
    per-message ingest path). They gate the summary MESSAGE ITSELF via `SummaryScheduler`,
    independent of `harmonic_detail_interval_s`'s existing gate on just the harmonic-detail
    sub-block. Default 10 s sits at the slow end of `03 S3.1`'s 2-10s L-RT telemetry-cadence
    band, still within S5.4's "same 2-10s cadence" continuous-monitoring requirement."""

    harmonic_detail_interval_s: float = 30.0
    harmonic_detail_delta_pct: float = 1.0
    raw_audit_sample_pct_per_min: float = 1.0
    sync_source: str = "ptp"
    sync_quality_ns: float = 50.0
    summary_interval_s: float = 10.0
    summary_delta_pct: float = 1.0
    # Source-impedance THD_V model (this module's docstring): short-circuit ratio of the
    # service point against one unit's rated current. 20 is a moderately stiff residential
    # service (IEEE 519-2014 Table 2's lowest band is ISC/IL < 20).
    source_short_circuit_ratio: float = 20.0
    unit_rated_kw: float = 11.0

    @property
    def source_reactance_ohm(self) -> float:
        """X_source at the fundamental: V_nom / (SCR x I_rated), I_rated = unit_rated_kw / V_nom."""
        rated_current_a = self.unit_rated_kw * 1000.0 / NOMINAL_VOLTAGE_V
        return NOMINAL_VOLTAGE_V / (self.source_short_circuit_ratio * rated_current_a)


# ---------------------------------------------------------------------------
# Summary message (fast sub-block every tick; harmonic-detail sub-block gated).
# ---------------------------------------------------------------------------


@dataclass
class HarmonicDetailScheduler:
    """S6.4b: the harmonic-detail sub-block (orders 2-50 mag/angle) publishes at a
    slower cadence (`interval_s`, default 30 s) OR on-change (aggregate THD moving by
    more than `delta_pct`) -- never every tick, which is what keeps the summary
    channel's steady-state bandwidth down from ~8 Mbps to ~3.4 Mbps at 10,000 hubs
    (S6.4b's own bandwidth math)."""

    interval_s: float
    delta_pct: float
    _last_at: dict[str, float] = field(default_factory=dict)
    _last_thd_pct: dict[str, float] = field(default_factory=dict)

    def due(self, hub_id: str, now: float, thd_current_pct: float) -> bool:
        last_at = self._last_at.get(hub_id)
        last_thd = self._last_thd_pct.get(hub_id)
        due_by_time = last_at is None or (now - last_at) >= self.interval_s
        due_by_delta = last_thd is not None and abs(thd_current_pct - last_thd) >= self.delta_pct
        if due_by_time or due_by_delta:
            self._last_at[hub_id] = now
            self._last_thd_pct[hub_id] = thd_current_pct
            return True
        return False


class SummaryScheduler(HarmonicDetailScheduler):
    """S9 wave-2 fix: gates the periodic PQ summary MESSAGE ITSELF (not just its harmonic-
    detail sub-block) to a configurable cadence (`WaveConfig.summary_interval_s`, default
    10 s) or on-change beyond `summary_delta_pct` of aggregate THD_I -- so a large fleet
    does not publish (and `opengrid.pq_ingest` does not have to buffer/insert) a full
    waveform summary every telemetry tick. Identical due()/interval+delta logic to
    `HarmonicDetailScheduler` (inherited, not re-implemented, BUILD.md S1) -- a SEPARATE
    instance from the harmonic-detail scheduler, since the two gate different messages on
    independent cadences/thresholds. The caller (`ogsim.fleet.runtime.FleetEngine`,
    sims-owned -- see the wave-2 build report's wiring notes) instantiates one of these
    per engine and calls `.due(hub_id, now, avg_thd_current_pct)` before building a hub's
    summary message at all, skipping the hub entirely (not just its harmonics) when not
    due."""


@dataclass
class _LegSum:
    """Phasor sums (A) of the units wired to one leg: fundamental and each harmonic order."""

    fundamental: complex = 0j
    harmonics: dict[int, complex] = field(default_factory=dict)
    voltage_offsets_pct: list[float] = field(default_factory=list)

    def add(self, snapshot: InverterSnapshot, current_a: float) -> None:
        self.fundamental += cmath.rect(current_a, math.radians(snapshot.phase_angle_error_deg))
        self.voltage_offsets_pct.append(snapshot.voltage_offset_pct)
        for order, component in snapshot.dominant_harmonics.items():
            phasor = cmath.rect(
                current_a * component["mag_pct"] / 100.0, math.radians(component["angle_deg"])
            )
            self.harmonics[order] = self.harmonics.get(order, 0j) + phasor


def _leg_sums(snapshots: Sequence[InverterSnapshot], unit_currents_a: Sequence[float]) -> dict[str, _LegSum]:
    """Groups a hub's units by leg (a unit wired to a leg pair contributes to each leg)."""
    legs: dict[str, _LegSum] = {}
    for snapshot, current_a in zip(snapshots, unit_currents_a, strict=True):
        for leg in _leg_suffixes(snapshot.phase_connection):
            legs.setdefault(leg, _LegSum()).add(snapshot, current_a)
    return legs


def _voltage_harmonics(current_harmonics: dict[int, complex], reactance_ohm: float) -> dict[int, complex]:
    """V_k = j k X_source I_k (V): the source-impedance THD_V model of this module's docstring."""
    return {order: 1j * order * reactance_ohm * phasor for order, phasor in current_harmonics.items()}


def _rss(phasors: dict[int, complex]) -> float:
    return math.sqrt(sum(abs(phasor) ** 2 for phasor in phasors.values()))


def _harmonics_block(phasors: dict[int, complex], fundamental: float) -> dict[str, dict[str, float]]:
    """Order -> {mag_pct of `fundamental`, angle_deg in [0, 360)} for the schema's harmonics block."""
    return {
        str(order): {
            "mag_pct": 100.0 * abs(phasor) / fundamental,
            "angle_deg": math.degrees(cmath.phase(phasor)) % _FULL_CIRCLE_DEG,
        }
        for order, phasor in sorted(phasors.items())
    }


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def build_summary_message(
    hub_id: str,
    bank_id: str,
    zone: str,
    snapshots: list[InverterSnapshot],
    ts: str,
    *,
    unit_currents_a: Sequence[float],
    include_harmonics: bool,
    config: WaveConfig,
) -> dict[str, Any]:
    """S6.4(a): one `pq_waveform_summary.schema.json` message per hub. `unit_currents_a` is each
    unit's dispatched fundamental RMS current (A), parallel to `snapshots`. Units sharing a leg
    are combined as phasors (this module's docstring), so a dual-unit home with both units on
    one leg reports the leg's full current and the vector-summed spectrum of both units.
    `include_harmonics` gates the harmonic-detail sub-block (`HarmonicDetailScheduler.due()`)."""
    msg: dict[str, Any] = {
        "hub_id": hub_id,
        "bank_id": bank_id,
        "zone": zone,
        "ts": ts,
        "sync_source": config.sync_source,
        "sync_quality_ns": config.sync_quality_ns,
    }
    reactance_ohm = config.source_reactance_ohm
    hub_fundamental_a = 0.0
    hub_harmonics: dict[int, complex] = {}
    v_rms_values: list[float] = []
    for leg, leg_sum in _leg_sums(snapshots, unit_currents_a).items():
        suffix = leg.lower()
        i_rms = abs(leg_sum.fundamental)
        v_rms = NOMINAL_VOLTAGE_V * (1.0 + _mean(leg_sum.voltage_offsets_pct) / 100.0)
        angle_deg = math.degrees(cmath.phase(leg_sum.fundamental))
        msg[f"v_rms_{suffix}"] = v_rms
        msg[f"i_rms_{suffix}"] = i_rms
        msg[f"pf_{suffix}"] = math.cos(math.radians(angle_deg))
        msg[f"phase_angle_deg_{suffix}"] = angle_deg
        msg[f"thd_i_pct_{suffix}"] = 100.0 * _rss(leg_sum.harmonics) / i_rms if i_rms > 0.0 else 0.0
        msg[f"thd_v_pct_{suffix}"] = (
            100.0 * _rss(_voltage_harmonics(leg_sum.harmonics, reactance_ohm)) / v_rms
        )
        hub_fundamental_a += i_rms
        v_rms_values.append(v_rms)
        for order, phasor in leg_sum.harmonics.items():
            hub_harmonics[order] = hub_harmonics.get(order, 0j) + phasor
    msg["freq_hz"] = _mean([s.freq_hz for s in snapshots]) if snapshots else NOMINAL_FREQ_HZ
    if include_harmonics and hub_fundamental_a > 0.0:
        msg["harmonics_i"] = _harmonics_block(hub_harmonics, hub_fundamental_a)
        msg["harmonics_v"] = _harmonics_block(
            _voltage_harmonics(hub_harmonics, reactance_ohm), _mean(v_rms_values)
        )
    return msg


def current_rms_a(output_kw: float, nominal_voltage_v: float = NOMINAL_VOLTAGE_V) -> float:
    """RMS current (A) for a hub dispatching `output_kw` (assumed PF~1 for this
    magnitude estimate; the summary's own `pf` field carries the actual phase-error-
    derived power factor), floored at `IDLE_CURRENT_A_FLOOR` so a near-zero dispatch
    still yields an analyzable (nonzero-fundamental) waveform."""
    return max(abs(output_kw) * 1000.0 / nominal_voltage_v, IDLE_CURRENT_A_FLOOR)


# ---------------------------------------------------------------------------
# Raw waveform capture (S6.4a/S7.4): sinusoid + dominant-harmonics synthesis.
# ---------------------------------------------------------------------------


def _harmonic_waveform(
    t: np.ndarray,
    freq_hz: float,
    amplitude: float,
    dominant_harmonics: dict[int, dict[str, float]],
    *,
    phase_deg_offset: float = 0.0,
) -> np.ndarray:
    """S7.4's v(t)/i(t) formula: a fundamental sinusoid plus each dominant-harmonic
    order's contribution (magnitude as %-of-fundamental, angle relative to the sync
    reference), with an optional constant phase offset (i(t)'s `phase_angle_error_deg`
    relative to v(t))."""
    total = amplitude * np.sin(2.0 * np.pi * freq_hz * t + math.radians(phase_deg_offset))
    for order, component in dominant_harmonics.items():
        mag = amplitude * component["mag_pct"] / 100.0
        theta = math.radians(component["angle_deg"] + phase_deg_offset)
        total = total + mag * np.sin(2.0 * np.pi * freq_hz * order * t + theta)
    return total


def _quantize_int16(samples: np.ndarray) -> list[int]:
    """S6.4(a): 16-bit signed integer samples. Physical volt/amp magnitudes here (peak
    a few hundred V, tens of A) sit far inside int16's range without a scale factor."""
    clipped = np.clip(np.round(samples), -32768, 32767)
    return [int(v) for v in clipped]


def synthesize_raw_capture(
    hub_id: str,
    phase_connection: str,
    snapshots: list[InverterSnapshot],
    output_kw: float,
    trigger_reason: str,
    ts: str,
    config: WaveConfig,
) -> dict[str, Any]:
    """S7.4: synthesizes one `pq_waveform_raw.schema.json` capture (the test/fixture
    JSON `samples` representation -- see that schema's docstring for why this sim
    never sends true binary on the wire) from `snapshots` (one per unit on this hub;
    each leg the hub covers uses its own unit's characterization when available, else
    falls back to the first unit's, mirroring `ogsim.fleet.pq`'s "representative unit"
    convention for a dual-unit home)."""
    channels = channels_for_phase_connection(phase_connection)
    t = np.arange(SAMPLES_PER_CAPTURE, dtype=np.float64) / SAMPLE_RATE_HZ
    by_leg = {snapshot.phase_connection: snapshot for snapshot in snapshots}
    fallback = snapshots[0]
    i_rms = current_rms_a(output_kw)
    samples: dict[str, list[int]] = {}
    for leg in _leg_suffixes(phase_connection):
        snapshot = by_leg.get(leg, fallback)
        v_amplitude = NOMINAL_VOLTAGE_V * math.sqrt(2.0) * (1.0 + snapshot.voltage_offset_pct / 100.0)
        i_amplitude = i_rms * math.sqrt(2.0)
        v_samples = _harmonic_waveform(t, snapshot.freq_hz, v_amplitude, snapshot.dominant_harmonics)
        i_samples = _harmonic_waveform(
            t,
            snapshot.freq_hz,
            i_amplitude,
            snapshot.dominant_harmonics,
            phase_deg_offset=snapshot.phase_angle_error_deg,
        )
        v_key, i_key = ("V", "I") if len(channels) == 2 else (f"V_{leg}", f"I_{leg}")
        samples[v_key] = _quantize_int16(v_samples)
        samples[i_key] = _quantize_int16(i_samples)
    return {
        "hub_id": hub_id,
        "phase_connection": phase_connection,
        "ts": ts,
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "cycles": CAPTURE_CYCLES,
        "channels": channels,
        "sync_source": config.sync_source,
        "sync_quality_ns": config.sync_quality_ns,
        "compression": "none",
        "trigger_reason": trigger_reason,
        "samples": samples,
    }


# ---------------------------------------------------------------------------
# Rotating audit sample and capture-request handling (S6.4b trigger policy).
# ---------------------------------------------------------------------------


class RotatingAuditSampler:
    """S6.4b trigger policy item (iii): a low-rate rotating audit sample (default
    1%/minute, round-robin by `hub_id` hash) so every hub is periodically spot-checked
    without sustaining full-fleet raw bandwidth. `due_hub_ids(hub_ids, now)` is a pure
    function of `(hub_id, minute_bucket)` -- deterministic and reproducible for a given
    seed/clock -- but the CALLER (`ogsim.fleet.runtime.run_fleet`'s tick loop) invokes it
    every telemetry tick (`config.telemetry_interval_s`, 2 s by default), not once a
    minute.

    **Bug fixed (post-deploy defect report, live 2026-09-26+N): a hub's `frac < threshold`
    result does not change while `minute_bucket` stays the same, so with no memory of
    "already emitted this bucket" the SAME ~1% of hubs was re-flagged due on every one of
    the ~30 ticks inside a 60 s window -- roughly 30x the intended rate (measured ~400/min
    against a ~20/min target at 2,000 hubs, S6.4b's "1% of the fleet per minute" cap).**
    This class now tracks the minute bucket it last emitted each hub for and skips a hub
    already emitted in the CURRENT bucket, so the total emitted per rolling minute stays at
    the intended ~1% of the fleet no matter how many times per minute the caller ticks.
    The underlying hash-based selection is unchanged (still exactly reproducible for a
    given seed/clock); only the "have I already told the caller about this one this
    minute" bookkeeping is new state."""

    def __init__(self, sample_pct_per_min: float) -> None:
        self.sample_pct_per_min = sample_pct_per_min
        self._last_emitted_bucket: dict[str, int] = {}

    def due_hub_ids(self, hub_ids: list[str], now: float) -> list[str]:
        if self.sample_pct_per_min <= 0.0:
            return []
        threshold = self.sample_pct_per_min / 100.0
        minute_bucket = int(now // 60.0)
        due = []
        for hub_id in hub_ids:
            if self._last_emitted_bucket.get(hub_id) == minute_bucket:
                continue  # already sampled this hub in this minute bucket (fix above)
            digest = hashlib.sha256(f"{hub_id}:{minute_bucket}".encode()).digest()
            frac = int.from_bytes(digest[:4], "big") / 2**32
            if frac < threshold:
                due.append(hub_id)
                self._last_emitted_bucket[hub_id] = minute_bucket
        return due


def capture_request_expired(request: dict[str, Any], now: float) -> bool:
    """S6.4b: "the hub ignores the request if it cannot capture and publish before
    this deadline" -- a request received after its own `expires_at` is dropped."""
    expires_at = parse_rfc3339(str(request["expires_at"]))
    now_dt = datetime.fromtimestamp(now, tz=UTC)
    return now_dt >= expires_at
