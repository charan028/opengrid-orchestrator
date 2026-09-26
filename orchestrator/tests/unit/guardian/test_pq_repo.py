"""`opengrid.guardian.pq_repo`'s Postgres-backed K14 port adapters (07-delivery/06 S5.3/S5.4/S6.7,
G-21..G-25), exercised against a minimal in-memory fake of the psycopg async pool/cursor protocol --
mirrors `tests/unit/guardian/test_repo.py`'s own fake (no real database, BUILD.md S5)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from opengrid.core.pq import DEFAULT_FIRMWARE_CALIBRATION_BOUNDS, CalibrationBounds
from opengrid.guardian import pq_repo

pytestmark = pytest.mark.asyncio


class FakeCursor:
    """Each `.execute()` call consumes the next canned response from `responses` (a row tuple/None for
    a fetchone-style query, or a list of rows for a fetchall-style query)."""

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.executed: list[tuple[str, dict]] = []
        self._current = None

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._current = self._responses.pop(0) if self._responses else None

    async def fetchone(self):
        return self._current

    async def fetchall(self):
        return self._current or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakePool:
    """Every `.connection()` call returns the SAME connection/cursor, so canned `responses` are
    consumed in call order across however many `async with pool.connection()` blocks a method uses
    (matches `tests/unit/guardian/test_repo.py`'s own pattern)."""

    def __init__(self, cursor: FakeCursor) -> None:
        self._conn = FakeConn(cursor)

    def connection(self):
        return self._conn


def _summary_row(hub_id: str, ts: datetime) -> tuple:
    """One `og.pq_waveform_summary` row, in `PgPqMeasurementPort._latest_summaries`'s own column
    order, with just enough fields set (v_rms_a, freq_hz) for `bank_measurement` to accept it."""
    return (
        hub_id, ts, 240.0, None, None, 10.0, None, None, 60.0, 0.98, None, None,
        1.0, None, None, 2.0, None, None, 0.5, None, None, None, None, "ptp", 50.0,
    )  # fmt: skip


# ---------------------------------------------------------------------------
# PgPqEnvelopeStatePort
# ---------------------------------------------------------------------------


async def test_tightest_active_limits_returns_none_when_no_sensitive_obligation():
    cursor = FakeCursor(responses=[[]])
    port = pq_repo.PgPqEnvelopeStatePort(FakePool(cursor))  # type: ignore[arg-type]
    assert await port.tightest_active_limits("bank-01") is None


async def test_tightest_active_limits_picks_tightest_across_obligations():
    # Two obligations behind the bank: one loose (imbalance 3.0, pf_min 0.90), one tight
    # (imbalance 1.0, pf_min 0.98) -- the tightest per-field combination must win, even
    # though it comes from two different rows.
    rows = [
        (3.0, 5.0, 0.5, 0.90, 5.0, 5.0, None),
        (1.0, 2.0, 0.2, 0.98, 3.0, 3.0, 100.0),
    ]
    cursor = FakeCursor(responses=[rows])
    port = pq_repo.PgPqEnvelopeStatePort(FakePool(cursor))  # type: ignore[arg-type]

    limits = await port.tightest_active_limits("bank-01")

    assert limits is not None
    assert limits.max_phase_imbalance_pct == 1.0
    assert limits.voltage_band_pct == 2.0
    assert limits.freq_tolerance_hz == 0.2
    assert limits.pf_min == 0.98  # tightest pf_min is the LARGER value
    assert limits.thd_voltage_limit_pct == 3.0
    assert limits.thd_current_limit_pct == 3.0
    assert limits.current_limit_a == 100.0


# ---------------------------------------------------------------------------
# PgPqMeasurementPort
# ---------------------------------------------------------------------------


async def test_aggregate_measurement_uses_measured_summaries_when_fresh():
    now = datetime.now(UTC)
    hub_ids_rows = [("hub-00000",)]
    summary_rows = [_summary_row("hub-00000", now)]
    cursor = FakeCursor(responses=[hub_ids_rows, summary_rows])
    port = pq_repo.PgPqMeasurementPort(FakePool(cursor))  # type: ignore[arg-type]

    measurement, is_stale = await port.aggregate_measurement("bank-01")

    assert is_stale is False
    assert measurement.thd_current_pct == pytest.approx(2.0)


async def test_aggregate_measurement_falls_back_to_modelled_characterization():
    hub_ids_rows = [("hub-00000",)]
    no_summaries: list = []
    hub_inverter_pq_rows = [
        ("hub-00000", 0.01, 0.5, 3.0, 0.9, 0.9, None),
    ]
    cursor = FakeCursor(responses=[hub_ids_rows, no_summaries, hub_inverter_pq_rows])
    port = pq_repo.PgPqMeasurementPort(FakePool(cursor))  # type: ignore[arg-type]

    measurement, is_stale = await port.aggregate_measurement("bank-01")

    assert is_stale is True
    assert measurement.freq_deviation_hz == pytest.approx(0.01)
    assert measurement.voltage_deviation_pct == pytest.approx(0.5)
    assert measurement.thd_current_pct == pytest.approx(3.0)


async def test_aggregate_measurement_conservative_when_nothing_available():
    """K1's "missing -> never compliant": with no measured summaries AND no
    characterization at all, every dimension reports at its worst rather than raising."""
    cursor = FakeCursor(responses=[[], []])
    port = pq_repo.PgPqMeasurementPort(FakePool(cursor))  # type: ignore[arg-type]

    measurement, is_stale = await port.aggregate_measurement("bank-01")

    assert is_stale is True
    assert measurement.pf == 0.0
    assert measurement.thd_current_pct == 100.0


async def test_aggregate_measurement_vector_sums_harmonics_when_available():
    hub_ids_rows = [("hub-00000",), ("hub-00001",)]
    no_summaries: list = []
    # Two hubs with harmonics at OPPOSITE phase angles -- vector cancellation should
    # yield a materially smaller THD than the naive scalar-sum fallback would.
    hub_inverter_pq_rows = [
        ("hub-00000", 0.0, 0.0, 4.0, 0.9, 0.9, {"3": {"mag_pct": 4.0, "angle_deg": 0.0}}),
        ("hub-00001", 0.0, 0.0, 4.0, 0.9, 0.9, {"3": {"mag_pct": 4.0, "angle_deg": 180.0}}),
    ]
    cursor = FakeCursor(responses=[hub_ids_rows, no_summaries, hub_inverter_pq_rows])
    port = pq_repo.PgPqMeasurementPort(FakePool(cursor))  # type: ignore[arg-type]

    measurement, is_stale = await port.aggregate_measurement("bank-01")

    assert is_stale is True
    assert measurement.thd_current_pct < 4.0  # cancellation, not the 8.0 scalar-stack value


# ---------------------------------------------------------------------------
# PgHubAssetStatePort
# ---------------------------------------------------------------------------


async def test_hub_asset_state_snapshot_found():
    cursor = FakeCursor(responses=[("DEGRADED", "CATEGORY_III")])
    port = pq_repo.PgHubAssetStatePort(FakePool(cursor))  # type: ignore[arg-type]

    snapshot = await port.snapshot("hub-00000")

    assert snapshot is not None
    assert snapshot.asset_state == "DEGRADED"
    assert snapshot.ride_through_class == "CATEGORY_III"


async def test_hub_asset_state_snapshot_unknown_hub_returns_none():
    cursor = FakeCursor(responses=[None])
    port = pq_repo.PgHubAssetStatePort(FakePool(cursor))  # type: ignore[arg-type]
    assert await port.snapshot("hub-99999") is None


# ---------------------------------------------------------------------------
# PgCalibrationHistoryPort
# ---------------------------------------------------------------------------


async def test_calibration_history_returns_epoch_seconds():
    cursor = FakeCursor(responses=[(1_700_000_000.0,)])
    port = pq_repo.PgCalibrationHistoryPort(FakePool(cursor))  # type: ignore[arg-type]
    assert await port.last_attempt_epoch_s("hub-00000") == pytest.approx(1_700_000_000.0)


async def test_calibration_history_none_when_never_attempted():
    cursor = FakeCursor(responses=[None])
    port = pq_repo.PgCalibrationHistoryPort(FakePool(cursor))  # type: ignore[arg-type]
    assert await port.last_attempt_epoch_s("hub-00000") is None


async def test_calibration_history_reads_the_guardians_own_signed_record_not_the_ladder_table():
    """Regression: the rate limit read `og.calibration_attempt`, whose newest row is always the PENDING
    candidate under evaluation (the ladder records it first), so G-25 refused every calibration."""
    cursor = FakeCursor(responses=[(None,)])
    port = pq_repo.PgCalibrationHistoryPort(FakePool(cursor))  # type: ignore[arg-type]

    assert await port.last_attempt_epoch_s("hub-00007") is None

    sql, params = cursor.executed[0]
    assert "og.calibration_attempt" not in sql
    assert "og.calibration_command" in sql and "'SIGNED'" in sql
    assert params == {"hub_id": "hub-00007"}


# ---------------------------------------------------------------------------
# PgCalibrationLedgerPort (#15 durable per-hub sequence, #20 atomic claim, #10 fleet usage)
# ---------------------------------------------------------------------------


async def test_ledger_reserve_locks_the_hub_and_claims_atomically():
    cursor = FakeCursor(responses=[None, (1, 7)])
    pool = FakePool(cursor)
    ledger = pq_repo.PgCalibrationLedgerPort(pool)  # type: ignore[arg-type]
    cid = UUID("00000000-0000-4000-8000-0000000000c1")

    assert await ledger.reserve(cid, "hub-1") == (1, 7)
    lock_sql, _ = cursor.executed[0]
    reserve_sql, params = cursor.executed[1]
    assert "pg_advisory_xact_lock" in lock_sql
    assert "ON CONFLICT (calibration_id) DO NOTHING" in reserve_sql and "MAX(seq)" in reserve_sql
    assert params == {"calibration_id": cid, "hub_id": "hub-1", "epoch": 1}


async def test_ledger_reserve_of_an_already_claimed_attempt_returns_none():
    ledger = pq_repo.PgCalibrationLedgerPort(FakePool(FakeCursor(responses=[None, None])))  # type: ignore[arg-type]
    assert await ledger.reserve(UUID(int=1), "hub-1") is None


async def test_ledger_fleet_usage_maps_the_counts():
    ledger = pq_repo.PgCalibrationLedgerPort(FakePool(FakeCursor(responses=[(2000, 12, 3, 9)])))  # type: ignore[arg-type]
    usage = await ledger.fleet_usage(window_s=3600.0)
    assert (usage.fleet_hubs, usage.signed_in_window, usage.in_flight, usage.flagged_hubs) == (2000, 12, 3, 9)


async def test_ledger_alert_is_raised_once_while_open(monkeypatch):
    raised: list = []

    async def fake_raise(pool, finding, *, opened_at):
        raised.append(finding.rule)
        return 1

    import opengrid.health.queries as health_queries

    monkeypatch.setattr(health_queries, "raise_alert", fake_raise)
    open_already = pq_repo.PgCalibrationLedgerPort(FakePool(FakeCursor(responses=[(1,)])))  # type: ignore[arg-type]
    await open_already.raise_alert("ALR-CALIBRATION-BUDGET", "s", {"reason": "x"})
    fresh = pq_repo.PgCalibrationLedgerPort(FakePool(FakeCursor(responses=[None])))  # type: ignore[arg-type]
    await fresh.raise_alert("ALR-CALIBRATION-BUDGET", "s", {"reason": "x"})
    assert raised == ["ALR-CALIBRATION-BUDGET"]


# ---------------------------------------------------------------------------
# StaticFirmwareCalibrationBoundsPort
# ---------------------------------------------------------------------------


async def test_static_firmware_bounds_returns_configured_defaults_for_any_hub():
    port = pq_repo.StaticFirmwareCalibrationBoundsPort()
    bounds = await port.max_bounds_for_hub("hub-00000")
    assert bounds == CalibrationBounds(max_freq_hz=0.10, max_voltage_pct=2.0, max_phase_deg=5.0)
    assert bounds == DEFAULT_FIRMWARE_CALIBRATION_BOUNDS


async def test_static_firmware_bounds_accepts_overrides():
    port = pq_repo.StaticFirmwareCalibrationBoundsPort(
        CalibrationBounds(max_freq_hz=0.05, max_voltage_pct=1.0, max_phase_deg=2.5)
    )
    bounds = await port.max_bounds_for_hub("hub-anything")
    assert bounds == CalibrationBounds(max_freq_hz=0.05, max_voltage_pct=1.0, max_phase_deg=2.5)


# ---------------------------------------------------------------------------
# PgSensitiveGrantPort
# ---------------------------------------------------------------------------


async def test_sensitive_grant_port_true_when_active_grant_found():
    cursor = FakeCursor(responses=[(1,)])
    port = pq_repo.PgSensitiveGrantPort(FakePool(cursor))  # type: ignore[arg-type]
    assert await port.has_active_non_default_envelope_grant("hub-00000") is True


async def test_sensitive_grant_port_false_when_no_grant_found():
    cursor = FakeCursor(responses=[None])
    port = pq_repo.PgSensitiveGrantPort(FakePool(cursor))  # type: ignore[arg-type]
    assert await port.has_active_non_default_envelope_grant("hub-00000") is False
