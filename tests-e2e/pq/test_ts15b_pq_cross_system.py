"""TS-15b: PQ cross-system test, ogsim waveform summaries -> opengrid measured bank aggregation.

Test plan: `docs/orchestrator/07-delivery/06-service-profiles-and-power-quality.md` S8.4, TS-15b ("for random
seeded per-hub harmonics, the orchestrator's measured aggregation (S6.5) from published summaries matches the
simulator's ground-truth vector sum within quantization tolerance (closes the S7.4 loop)").

Data path, in process, no broker (BUILD.md S6):

    ogsim.fleet.runtime.FleetEngine (seeded InverterPqState, S7.1)
      -> FleetEngine.wave_summary_messages() (ogsim.fleet.wave.build_summary_message, S6.4a/S7.4)
      -> json.dumps / json.loads (the wire)
      -> schema check on both sides (ogsim.common.schemas.validate, opengrid.platform.mqtt.validate_payload)
      -> opengrid.pq_ingest.ingest_summary + flush_summaries (the engine's real ingest path, in-memory backend)
      -> opengrid.pq_ingest.aggregation.bank_measurement (S6.5 step 3)

Ground truth is computed here, independently, from ogsim's seeded per-unit state (`FleetEngine.pq`) and the
dispatched kW, using the S3.2 formulas. Every test runs twice: on single-unit homes (`dual_unit_share=0`) and on
Base's confirmed mix (`dual_unit_share=0.2`, BUILD.md S2a). For a dual-unit home the ground truth combines the
two units per leg as phasors (fundamental and each harmonic order), exactly what a revenue-grade edge meter on
that leg measures, and the hub's harmonic block must be the vector sum of both units (WP-K, #16 follow-ups).

This is the only suite that imports both products; it lives in `tests-e2e/`, never in either package (BUILD.md
S1, `orchestrator/tools/dupcheck.py`). Run from the repository root:

    python -m pytest tests-e2e/pq -q
"""

from __future__ import annotations

import asyncio
import cmath
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from ogsim.common.config import FleetConfig, MqttSettings
from ogsim.common.schemas import validate as ogsim_validate
from ogsim.fleet.runtime import FleetEngine
from ogsim.fleet.wave import WaveConfig, current_rms_a
from opengrid import pq_ingest
from opengrid.core.models.pq import PqWaveformRawIndex, PqWaveformSummaryRow
from opengrid.core.pq import (
    ComplianceState,
    PqEnvelopeLimits,
    PqMeasurement,
    bank_thd_current_pct,
    evaluate_envelope,
)
from opengrid.platform.mqtt import validate_payload
from opengrid.pq_ingest.aggregation import bank_measurement
from opengrid.pq_ingest.blob_store import FileBlobStore
from opengrid.pq_ingest.characterize import HubCharacterization

# One feeder-segment bank (S3.2's "bank of 50 single-phase hubs" scale); 30 keeps the suite well under a second.
HUB_COUNT = 30
BANK_ID = "bank-000"
ZONE = "LZ_NORTH"
SEEDS = (15, 1515, 20260926)
NOW = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)

# S5.4: a summary older than 2x the telemetry (summary) cadence is missing, never compliant.
FRESHNESS_S = 2.0 * FleetConfig.wave_summary_interval_s

# Scalar summary fields travel as JSON numbers (pq_waveform_summary.schema.json), so today the wire is lossless
# and any disagreement beyond float round-off is a real defect, not quantization.
SCALAR_REL_TOL = 1e-9
SCALAR_ABS_TOL = 1e-9

# S6.4a harmonic encoding: scaled int16, 0.01 % of fundamental for magnitude and 0.1 deg for angle. TS-15b's
# "quantization tolerance" is the worst case those resolutions allow on the bank THD_I (see `_thd_tolerance_pct`).
HARMONIC_MAG_LSB_PCT = 0.01
HARMONIC_ANGLE_LSB_DEG = 0.1

# ogsim's voltage-distortion model (ogsim.fleet.wave module docstring): each harmonic current drives
# V_k = j k X_source I_k across the service's source reactance, X_source = V_nom / (SCR x I_rated).
SOURCE_REACTANCE_OHM = WaveConfig().source_reactance_ohm
NOMINAL_VOLTAGE_V = 240.0

# 0.2 is Base's confirmed share of two-unit homes (BUILD.md S2a); 0.0 keeps the one-summary-per-inverter case.
DUAL_UNIT_SHARES = (0.0, 0.2)

# S2 HOME defaults (ANSI C84.1 Range A, IEEE 1547-2018, IEEE 519-2014). Imbalance is "n/a (single customer)" for
# HOME, so it is left unbounded here.
HOME_ENVELOPE = PqEnvelopeLimits(
    max_phase_imbalance_pct=math.inf,
    voltage_band_pct=5.0,
    freq_tolerance_hz=0.5,
    pf_min=0.90,
    thd_voltage_limit_pct=5.0,
    thd_current_limit_pct=5.0,
)

_TEST_MQTT = MqttSettings(host="127.0.0.1", port=1883, username="unused", password="", topic_root="og/v1")


# --- ogsim side: a seeded bank publishing its summaries ---------------------------------------------------------


def _scenario(anomaly_id: str, wire_type: str, kind: str, ref: str, params: dict[str, Any]) -> dict[str, Any]:
    """A `scenario_control.schema.json` command, applied in process (the same parser the control plane feeds)."""
    return {
        "id": anomaly_id,
        "type": wire_type,
        "target": {"kind": kind, "ref": ref},
        "params": params,
        "start": NOW.timestamp(),
    }


@pytest.fixture(params=DUAL_UNIT_SHARES, ids=lambda share: f"dual{share:g}")
def dual_unit_share(request: pytest.FixtureRequest) -> float:
    share: float = request.param
    return share


def _seeded_bank(
    seed: int,
    dual_unit_share: float,
    *,
    phase_lock: bool = False,
    scenarios: Sequence[dict[str, Any]] = (),
) -> FleetEngine:
    """One bank of single-unit homes, seeded per S7.1, with a seeded dispatch so per-phase currents differ.

    The dispatch is written straight to `p_kw_applied` after the tick: it stands in for a signed command batch
    (that path is TS-04's), and it is the only live-dispatch number the summary reads (`set_current_rms`)."""
    config = FleetConfig(
        mqtt=_TEST_MQTT,
        hub_count=HUB_COUNT,
        bank_count=1,
        zones=(ZONE,),
        dual_unit_share=dual_unit_share,
        pq_harmonic_phase_lock=phase_lock,
    )
    engine = FleetEngine(config, seed=seed)
    for command in scenarios:
        assert engine.handle_scenario_cmd(command) is not None, command
    engine.tick(NOW.timestamp())
    dispatch_rng = np.random.default_rng(seed + 1)
    engine.state.p_kw_applied[:] = dispatch_rng.uniform(0.0, config.p_kw_default, size=HUB_COUNT)
    return engine


def _published_payloads(engine: FleetEngine) -> list[dict[str, Any]]:
    """Every summary the bank publishes this period, serialized and re-parsed exactly as the wire would."""
    payloads = []
    for _topic, message in engine.wave_summary_messages(NOW.timestamp()):
        wire = json.loads(json.dumps(message))
        ogsim_validate("pq_waveform_summary", wire)
        payloads.append(wire)
    return payloads


# --- opengrid side: the engine's ingest path into an in-memory store ---------------------------------------------


class _InMemoryPqBackend:
    """`opengrid.pq_ingest.PqIngestBackend` without Postgres: keeps what a flush writes."""

    def __init__(self) -> None:
        self.summaries: list[PqWaveformSummaryRow] = []

    async def insert_summary(self, row: PqWaveformSummaryRow) -> None:
        self.summaries.append(row)

    async def insert_summaries_batch(self, rows: Sequence[PqWaveformSummaryRow]) -> None:
        self.summaries.extend(rows)

    async def insert_raw_index(self, row: PqWaveformRawIndex) -> None:
        raise AssertionError("TS-15b publishes summaries only")

    async def latest_summaries(
        self, hub_ids: Sequence[str], *, since: datetime
    ) -> list[PqWaveformSummaryRow]:
        return [row for row in self.summaries if row.hub_id in hub_ids and row.ts >= since]

    async def upsert_hub_inverter_pq_batch(self, rows: Sequence[HubCharacterization]) -> None:
        raise AssertionError("TS-15b does not characterize")


async def _ingest(payloads: Sequence[dict[str, Any]]) -> None:
    for payload in payloads:
        validate_payload("pq_waveform_summary", payload)
        await pq_ingest.ingest_summary(payload)
    await pq_ingest.flush_summaries()


def _ingested_rows(engine: FleetEngine, blob_dir: Path) -> list[PqWaveformSummaryRow]:
    """ogsim publish -> wire -> opengrid validate/parse/buffer/flush; returns the rows the store received."""
    backend = _InMemoryPqBackend()
    pq_ingest.configure(backend, FileBlobStore(str(blob_dir)))
    asyncio.run(_ingest(_published_payloads(engine)))
    return backend.summaries


# --- ground truth from ogsim's seeded state (S3.2) ----------------------------------------------------------------


@dataclass(frozen=True)
class BankTruth:
    """What the bank physically does, per S3.2, from the simulator's own per-unit parameters."""

    hub_ids: tuple[str, ...]
    voltage_deviation_pct: float
    freq_deviation_hz: float
    pf: float
    thd_voltage_pct: float
    current_a: float
    imbalance_pct: float
    thd_current_vector_sum_pct: float
    max_harmonic_mag_pct: float
    harmonic_orders: int


@dataclass(frozen=True)
class LegTruth:
    """One hub leg as physics has it: every unit on the leg summed as phasors (A)."""

    phase: str
    fundamental: complex
    harmonics: dict[int, complex]
    voltage_offset_pct: float

    @property
    def thd_voltage_pct(self) -> float:
        v_rms = NOMINAL_VOLTAGE_V * (1.0 + self.voltage_offset_pct / 100.0)
        v_sq = sum((order * SOURCE_REACTANCE_OHM * abs(i_k)) ** 2 for order, i_k in self.harmonics.items())
        return 100.0 * math.sqrt(v_sq) / v_rms


def _hub_legs(engine: FleetEngine, hub_id: str) -> list[LegTruth]:
    """The hub's dispatch splits equally over its units (one or two); units sharing a leg add as phasors."""
    pq, state = engine.pq, engine.state
    units = pq.indices_for_hub(hub_id)
    unit_current_a = current_rms_a(float(state.p_kw_applied[state.hub_index[hub_id]]) / len(units))
    legs: dict[str, list[int]] = {}
    for unit in units:
        legs.setdefault(pq.phase_connection[unit], []).append(unit)
    return [
        LegTruth(
            phase=phase,
            fundamental=sum(
                (
                    cmath.rect(unit_current_a, math.radians(float(pq.phase_angle_error_deg[u])))
                    for u in members
                ),
                0j,
            ),
            harmonics={
                order: sum(
                    (
                        cmath.rect(
                            unit_current_a * float(pq.harmonic_mag_pct[order][u]) / 100.0,
                            math.radians(float(pq.harmonic_angle_deg[order][u])),
                        )
                        for u in members
                    ),
                    0j,
                )
                for order in pq.harmonic_mag_pct
            },
            voltage_offset_pct=float(np.mean(pq.voltage_offset_pct[members])),
        )
        for phase, members in legs.items()
    ]


def _hub_current_a(engine: FleetEngine, hub_id: str) -> float:
    return sum(abs(leg.fundamental) for leg in _hub_legs(engine, hub_id))


def _ground_truth(engine: FleetEngine, excluded_hub_ids: Sequence[str] = ()) -> BankTruth:
    pq, state = engine.pq, engine.state
    hub_ids = tuple(h for h in state.hub_ids if h not in excluded_hub_ids)
    units = [u for h in hub_ids for u in pq.indices_for_hub(h)]
    legs = [leg for h in hub_ids for leg in _hub_legs(engine, h)]

    per_phase: dict[str, float] = {}
    for leg in legs:
        per_phase[leg.phase] = per_phase.get(leg.phase, 0.0) + abs(leg.fundamental)
    phase_currents = np.array(list(per_phase.values()))
    mean_phase_current = float(phase_currents.mean())
    imbalance = (
        100.0 * float(np.max(np.abs(phase_currents - mean_phase_current))) / mean_phase_current
        if len(per_phase) >= 2
        else 0.0
    )
    bank_current_a = float(phase_currents.sum())

    # S3.2(b): each order's bank phasor is the vector sum of every unit's I_u * m_k,u at angle theta_k,u; THD_I is
    # their RSS over the bank's summed fundamental.
    harmonic_sq = sum(
        abs(sum((leg.harmonics[order] for leg in legs), 0j)) ** 2 for order in pq.harmonic_mag_pct
    )
    hub_freq_offsets = [float(np.mean(pq.freq_offset_hz[pq.indices_for_hub(h)])) for h in hub_ids]

    return BankTruth(
        hub_ids=hub_ids,
        # S3.2(a): offsets average; a systematic bias does not cancel. The bank figure is the mean over the legs
        # (voltage, PF, THD_V) or hubs (frequency) that report one.
        voltage_deviation_pct=abs(float(np.mean([leg.voltage_offset_pct for leg in legs]))),
        freq_deviation_hz=abs(float(np.mean(hub_freq_offsets))),
        pf=float(np.mean([math.cos(cmath.phase(leg.fundamental)) for leg in legs])),
        thd_voltage_pct=float(np.mean([leg.thd_voltage_pct for leg in legs])),
        current_a=bank_current_a,
        imbalance_pct=imbalance,
        thd_current_vector_sum_pct=100.0 * math.sqrt(harmonic_sq) / bank_current_a,
        max_harmonic_mag_pct=max(float(np.max(mags[units])) for mags in pq.harmonic_mag_pct.values()),
        harmonic_orders=len(pq.harmonic_mag_pct),
    )


def _thd_tolerance_pct(truth: BankTruth) -> float:
    """Worst-case bank THD_I error from S6.4a quantization: every per-hub phasor may be off by half an LSB in
    magnitude plus half an LSB in angle (as a fraction of its magnitude); the per-order error of a
    current-weighted sum is bounded by the same per-unit bound, and RSS over the orders adds sqrt(orders)."""
    per_order = HARMONIC_MAG_LSB_PCT / 2 + truth.max_harmonic_mag_pct * math.radians(
        HARMONIC_ANGLE_LSB_DEG / 2
    )
    return math.sqrt(truth.harmonic_orders) * per_order


def _measured_vector_sum_thd_pct(rows: Sequence[PqWaveformSummaryRow]) -> float:
    """The orchestrator's canonical S3.2(b) formula (`opengrid.core.pq.bank_thd_current_pct`) on what arrived over
    the wire: each hub's fundamental is its summed per-leg `i_rms`, its phasors come from `harmonics_i`."""
    spectra, fundamentals = [], []
    for row in rows:
        assert row.harmonics_i is not None, (
            f"{row.hub_id}: harmonic-detail sub-block missing on first publish"
        )
        fundamental = sum(float(i) for i in (row.i_rms_a, row.i_rms_b, row.i_rms_c) if i is not None)
        spectra.append(
            {
                int(order): fundamental
                * float(c.mag_pct)
                / 100.0
                * cmath.exp(1j * math.radians(float(c.angle_deg)))
                for order, c in row.harmonics_i.items()
            }
        )
        fundamentals.append(fundamental)
    return bank_thd_current_pct(spectra, fundamentals)


def _measure(rows: Sequence[PqWaveformSummaryRow], now: datetime = NOW) -> PqMeasurement:
    measurement = bank_measurement(rows, now, FRESHNESS_S)
    assert measurement is not None, "fresh summaries with V and f must yield a measurement (S6.5 step 3)"
    return measurement


def _assert_scalars_match(measurement: PqMeasurement, truth: BankTruth) -> None:
    def close(expected: float) -> Any:
        return pytest.approx(expected, rel=SCALAR_REL_TOL, abs=SCALAR_ABS_TOL)

    assert measurement.voltage_deviation_pct == close(truth.voltage_deviation_pct)
    assert measurement.freq_deviation_hz == close(truth.freq_deviation_hz)
    assert measurement.pf == close(truth.pf)
    assert measurement.thd_voltage_pct == close(truth.thd_voltage_pct)
    assert measurement.current_a == close(truth.current_a)
    assert measurement.imbalance_pct == close(truth.imbalance_pct)


# --- tests ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("seed", SEEDS)
def test_ts_15b_bank_measurement_matches_seeded_ground_truth(
    dual_unit_share: float, seed: int, tmp_path: Path
) -> None:
    """S6.5 step 3 on published summaries reproduces the S3.2(a)/(c) bank figures from the seeded state."""
    engine = _seeded_bank(seed, dual_unit_share)
    rows = _ingested_rows(engine, tmp_path)
    truth = _ground_truth(engine)

    assert sorted(row.hub_id for row in rows) == sorted(truth.hub_ids)
    assert truth.imbalance_pct > 0.0, "the seeded dispatch must load the phases unequally"
    _assert_scalars_match(_measure(rows), truth)


@pytest.mark.parametrize("phase_lock", [False, True], ids=["diverse", "locked"])
@pytest.mark.parametrize("seed", SEEDS)
def test_ts_15b_published_harmonics_vector_sum_matches_ground_truth(
    dual_unit_share: float, seed: int, phase_lock: bool, tmp_path: Path
) -> None:
    """The per-hub harmonic phasors survive the wire: opengrid's own S3.2(b) vector sum over the ingested rows
    equals the vector sum of ogsim's seeded harmonics, in both the cancellation and the stacking regime."""
    engine = _seeded_bank(seed, dual_unit_share, phase_lock=phase_lock)
    rows = _ingested_rows(engine, tmp_path)
    truth = _ground_truth(engine)

    measured = _measured_vector_sum_thd_pct(rows)
    assert measured == pytest.approx(truth.thd_current_vector_sum_pct, abs=_thd_tolerance_pct(truth))


@pytest.mark.parametrize("seed", SEEDS)
def test_ts_15b_stacking_regime_exceeds_cancellation_regime(dual_unit_share: float, seed: int) -> None:
    """S3.2(b): with identical harmonic angles the bank THD_I does not fall with fleet size; with i.i.d. uniform
    angles it falls roughly as 1/sqrt(N). Same seed, same magnitudes, only the angles differ."""
    diverse = _ground_truth(_seeded_bank(seed, dual_unit_share, phase_lock=False))
    locked = _ground_truth(_seeded_bank(seed, dual_unit_share, phase_lock=True))
    assert locked.thd_current_vector_sum_pct > 2.0 * diverse.thd_current_vector_sum_pct


# Was a strict xfail (#16): bank_measurement reported THD_I as a per-hub mean. Fixed on main in R2, so it is
# now a plain regression test that the bank THD_I is the S3.2(b) vector sum.
@pytest.mark.parametrize("seed", SEEDS[:1])
def test_ts_15b_bank_measurement_thd_current_is_the_vector_sum(
    dual_unit_share: float, seed: int, tmp_path: Path
) -> None:
    engine = _seeded_bank(seed, dual_unit_share)
    truth = _ground_truth(engine)
    measurement = _measure(_ingested_rows(engine, tmp_path))
    assert measurement.thd_current_pct == pytest.approx(
        truth.thd_current_vector_sum_pct, abs=_thd_tolerance_pct(truth)
    )


@pytest.mark.parametrize("seed", SEEDS)
def test_ts_15b_fleetwide_harmonic_injection_breaches_home_thd_envelope(
    dual_unit_share: float, seed: int, tmp_path: Path
) -> None:
    """Negative case: a firmware-homogeneous (phase-locked, S3.2b stacking) bank whose every unit gets the
    `harmonic_injection` anomaly (S7.2, default 8 % THD_I on the 5th) breaches the HOME THD_I limit (S2: 5 %), both
    by the orchestrator's measurement and by the vector-sum ground truth; the same bank before injection does not."""
    injection = _scenario(
        "ts15b-hi", "FLEET_HARMONIC_INJECTION", "sim", "*", {"thd_target_pct": 8.0, "order": 5}
    )
    baseline = _seeded_bank(seed, dual_unit_share, phase_lock=True)
    injected = _seeded_bank(seed, dual_unit_share, phase_lock=True, scenarios=[injection])
    baseline_measurement = _measure(_ingested_rows(baseline, tmp_path / "baseline"))
    injected_rows = _ingested_rows(injected, tmp_path / "injected")
    injected_measurement = _measure(injected_rows)
    truth = _ground_truth(injected)

    assert evaluate_envelope(baseline_measurement, HOME_ENVELOPE)["thd_current_pct"] != ComplianceState.BREACH
    assert truth.thd_current_vector_sum_pct > HOME_ENVELOPE.thd_current_limit_pct
    assert _measured_vector_sum_thd_pct(injected_rows) == pytest.approx(
        truth.thd_current_vector_sum_pct, abs=_thd_tolerance_pct(truth)
    )
    assert evaluate_envelope(injected_measurement, HOME_ENVELOPE)["thd_current_pct"] == ComplianceState.BREACH
    _assert_scalars_match(injected_measurement, truth)


@pytest.mark.parametrize("seed", SEEDS)
def test_ts_15b_dropped_hub_is_excluded_not_treated_as_compliant(
    dual_unit_share: float, seed: int, tmp_path: Path
) -> None:
    """Negative case: a hub that stops publishing (`hub_offline`, S7.2) is absent from the aggregate, which then
    matches the ground truth of the remaining hubs; the bank current drops by exactly that hub's current. Once
    every summary is older than the S5.4 freshness window the bank has no measurement at all (K1), not a
    compliant one."""
    reference = _seeded_bank(seed, dual_unit_share)
    heaviest = reference.state.hub_ids[int(np.argmax(reference.state.p_kw_applied))]
    dropped = _seeded_bank(
        seed, dual_unit_share, scenarios=[_scenario("ts15b-off", "FLEET_HUB_OFFLINE", "hub", heaviest, {})]
    )
    rows = _ingested_rows(dropped, tmp_path)
    full = _ground_truth(dropped)
    truth = _ground_truth(dropped, excluded_hub_ids=[heaviest])

    assert heaviest not in {row.hub_id for row in rows}
    assert len(rows) == HUB_COUNT - 1
    measurement = _measure(rows)
    _assert_scalars_match(measurement, truth)
    assert full.current_a - measurement.current_a == pytest.approx(
        _hub_current_a(dropped, heaviest),
        rel=SCALAR_REL_TOL,
    )
    assert bank_measurement(rows, NOW + timedelta(seconds=FRESHNESS_S + 1.0), FRESHNESS_S) is None


@pytest.mark.parametrize("seed", SEEDS)
def test_ts_15b_dual_unit_home_summary_carries_both_units(seed: int, tmp_path: Path) -> None:
    """WP-K (#16 follow-ups): a two-unit home's summary is a lossless image of both units at the hub level: its
    per-leg currents sum to both units' phasor-summed current (the second unit on a shared leg no longer
    overwrites the first), and its harmonic block times that current is the vector sum of both units' spectra."""
    engine = _seeded_bank(seed, max(DUAL_UNIT_SHARES))
    rows = {row.hub_id: row for row in _ingested_rows(engine, tmp_path)}
    dual_hubs = [h for h in engine.state.hub_ids if len(engine.pq.indices_for_hub(h)) == 2]
    same_leg = [
        h
        for h in dual_hubs
        if len({engine.pq.phase_connection[u] for u in engine.pq.indices_for_hub(h)}) == 1
    ]
    assert dual_hubs and same_leg, "the seeded bank must include a two-unit home with both units on one leg"

    for hub_id in dual_hubs:
        row = rows[hub_id]
        legs = _hub_legs(engine, hub_id)
        fundamental = sum(float(i) for i in (row.i_rms_a, row.i_rms_b, row.i_rms_c) if i is not None)
        assert fundamental == pytest.approx(_hub_current_a(engine, hub_id), rel=SCALAR_REL_TOL)
        assert row.harmonics_i is not None
        for order, component in row.harmonics_i.items():
            published = cmath.rect(
                fundamental * float(component.mag_pct) / 100.0, math.radians(float(component.angle_deg))
            )
            expected = sum((leg.harmonics[int(order)] for leg in legs), 0j)
            assert abs(published - expected) == pytest.approx(0.0, abs=1e-9)
