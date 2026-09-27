"""`opengrid.guardian.repo`'s Postgres-backed port adapters, exercised against a minimal in-memory fake
of the psycopg async pool/cursor protocol (no real database -- BUILD.md S5's "Local: unit and property
tests, no DB/MQTT")."""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from opengrid.core.pq import OffsetVector
from opengrid.guardian import repo


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

    @property
    def rowcount(self) -> int:
        return len(self._current) if isinstance(self._current, list) else 0

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

    async def execute(self, sql, params=None):
        await self._cursor.execute(sql, params)
        return self._cursor

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, cursor: FakeCursor) -> None:
        self._conn = FakeConn(cursor)

    def connection(self):
        return self._conn


async def test_ts_06_09_ledger_version_port_reads_the_durable_version_not_the_engine_facade():
    """Regression (live 2026-09-26): the guardian delegated to `opengrid.ledger.ledger_version()`, the
    og-engine process's in-memory facade, which is never configured in og-guardian -- every evaluation
    raised RuntimeError. The port reads `og.reservation` through the ledger backend's own query."""
    import opengrid.ledger as ledger_module

    ledger_module._instance = None  # og-guardian never configures the engine facade
    cursor = FakeCursor([(41,)])
    ports, _leases = repo.build_pg_ports(
        FakePool(cursor),
        trace_store=None,  # type: ignore[arg-type]
        hubs=None,  # type: ignore[arg-type]
        clock=object(),  # type: ignore[arg-type]
        l2_instructions=object(),  # type: ignore[arg-type]
    )

    assert await ports.ledger.ledger_version() == 41
    assert "MAX(ledger_version)" in cursor.executed[0][0]


async def test_pg_bank_state_port_found_with_load():
    cursor = FakeCursor([(75.0, 5.0, "feeder-1"), (42.0, 1.5)])
    port = repo.PgBankStatePort(FakePool(cursor))
    snap = await port.snapshot("bank-1")
    assert snap is not None
    assert snap.feeder_id == "feeder-1"
    assert snap.bank_load_kva == 42.0
    assert snap.bank_load_age_s == 1.5


async def test_pg_bank_state_port_missing_bank():
    cursor = FakeCursor([None])
    port = repo.PgBankStatePort(FakePool(cursor))
    assert await port.snapshot("nope") is None


def test_g03_bank_load_reads_only_good_quality_scada():
    """A SCADA reading marked ESTIMATED/STALE is ignored (treated as no reading, i.e. stale), never as 0 kVA."""
    assert "quality = 'GOOD'" in repo._BANK_LOAD_SQL


async def test_pg_bank_state_port_no_load_reading_is_unknown_not_empty():
    """K4 regression: a bank with no SCADA reading used to look like a 0 kVA bank to G-03 (fail-open).
    It now carries an infinite reading age, which G-03 vetoes as BANK_LOAD_STALE."""
    cursor = FakeCursor([(75.0, 5.0, None), None])
    port = repo.PgBankStatePort(FakePool(cursor))
    snap = await port.snapshot("bank-1")
    assert snap is not None
    assert snap.bank_load_kva == 0.0
    assert snap.bank_load_age_s == math.inf


async def test_pg_commitment_port_found_and_default():
    obligation_id = uuid4()
    found = repo.PgCommitmentPort(FakePool(FakeCursor([(Decimal("5.0"),)])))
    assert await found.active_kw(obligation_id, "cycle-1") == Decimal("5.0")

    missing = repo.PgCommitmentPort(FakePool(FakeCursor([None])))
    assert await missing.active_kw(obligation_id, "cycle-1") == Decimal(0)


async def test_pg_commitment_port_active_obligations_for_bank():
    """GUARD-01: guardian's own enumeration reads og.reservation/og.commitment directly, independent of
    anything a proposed batch claims."""
    obligation_id = uuid4()
    cursor = FakeCursor([[(obligation_id, Decimal("5.0"), Decimal("8.0"))]])
    port = repo.PgCommitmentPort(FakePool(cursor))
    obligations = await port.active_obligations_for_bank("bank-1", "cycle-1")
    assert obligations == [
        repo.ActiveObligation(
            obligation_id=obligation_id, frozen_kw=Decimal("5.0"), total_frozen_kw=Decimal("8.0")
        )
    ]


async def test_pg_commitment_port_active_obligations_for_bank_none_active():
    port = repo.PgCommitmentPort(FakePool(FakeCursor([[]])))
    assert await port.active_obligations_for_bank("bank-1", "cycle-1") == []


async def test_pg_prior_grant_port_found_and_missing():
    obligation_id = uuid4()
    found = repo.PgPriorGrantPort(FakePool(FakeCursor([(Decimal("3.0"),)])))
    assert await found.prior_granted_kw(obligation_id) == Decimal("3.0")

    missing = repo.PgPriorGrantPort(FakePool(FakeCursor([None])))
    assert await missing.prior_granted_kw(obligation_id) is None


async def test_pg_lease_state_defaults_to_zero_zero():
    leases = repo.PgLeaseStatePort(FakePool(FakeCursor([None])))
    assert await leases.last_accepted("bank-1") == (0, 0)


async def test_pg_lease_state_reads_back_recorded_value():
    leases = repo.PgLeaseStatePort(FakePool(FakeCursor([(3, 7)])))
    assert await leases.last_accepted("bank-1") == (3, 7)


async def test_pg_lease_state_record_accepted_writes_and_commits():
    cursor = FakeCursor([None])
    pool = FakePool(cursor)
    leases = repo.PgLeaseStatePort(pool)
    await leases.record_accepted("bank-1", 3, 7)
    sql, params = cursor.executed[0]
    assert "lease_state" in sql
    assert params == {"bank_id": "bank-1", "epoch": 3, "seq": 7}
    assert pool._conn.committed is True


async def test_pg_lease_state_record_accepted_never_raises_on_db_failure():
    class RaisingCursor(FakeCursor):
        async def execute(self, sql, params=None):
            raise RuntimeError("db is down")

    leases = repo.PgLeaseStatePort(FakePool(RaisingCursor([])))
    await leases.record_accepted("bank-1", 3, 7)  # must not raise (K7: degrade, don't trip)


def test_pg_l2_instruction_port_removed():
    """K5: the Postgres L2 port read `payload ->> 'kind'` from RT_ALLOCATION traces the engine never
    writes (live: 0 rows in 24 h), so G-15 and any L2 override check always saw "no instruction". The
    guardian now subscribes to utility instructions itself (`mqtt_io.MqttL2InstructionPort`)."""
    assert not hasattr(repo, "PgL2InstructionPort")


async def test_pg_safe_stop_port_engaged_and_clear():
    engaged = repo.PgSafeStopPort(FakePool(FakeCursor([("ENGAGE",)])))
    assert await engaged.is_stopped("BANK", "bank-1") is True

    released = repo.PgSafeStopPort(FakePool(FakeCursor([("RELEASE",)])))
    assert await released.is_stopped("BANK", "bank-1") is False

    never = repo.PgSafeStopPort(FakePool(FakeCursor([None])))
    assert await never.is_stopped("BANK", "bank-1") is False


def _proposal_payload(command_batch_id) -> dict:
    return {
        "command_batch_id": str(command_batch_id),
        "bank_id": "bank-1",
        "cycle_id": "cycle-1",
        "epoch": 1,
        "seq": 1,
        "issued_at": "2026-09-26T18:00:00+00:00",
        "expires_at": "2026-09-26T18:00:10+00:00",
        "ledger_version": 1,
        "is_firm_event": False,
        "items": [
            {
                "hub_id": "hub-1",
                "p_kw_setpoint": 3.0,
                "reason_code": "SELECTOR",
                "obligation_id": None,
                "obligation_granted_kw": None,
            }
        ],
    }


async def test_pg_proposal_port_found():
    command_batch_id = uuid4()
    cursor = FakeCursor([(_proposal_payload(command_batch_id),)])
    port = repo.PgProposalPort(FakePool(cursor))
    proposal = await port.fetch(command_batch_id)
    assert proposal is not None
    assert proposal.bank_id == "bank-1"
    assert proposal.items[0].hub_id == "hub-1"


async def test_pg_proposal_port_missing():
    port = repo.PgProposalPort(FakePool(FakeCursor([None])))
    assert await port.fetch(uuid4()) is None


async def test_pg_proposal_port_malformed_payload_returns_none():
    cursor = FakeCursor([({"bank_id": "bank-1"},)])  # missing required keys
    port = repo.PgProposalPort(FakePool(cursor))
    assert await port.fetch(uuid4()) is None


async def test_load_hub_params():
    cursor = FakeCursor(
        [
            [
                ("hub-1", 39.2, 7.84, 11.0, 0.9487, 0.9487, 1, False),
                ("hub-2", 78.4, 15.68, 20.0, 0.9487, 0.9487, 2, False),
                ("sub-LZ_AEN-00", 40000.0, 8000.0, 20000.0, 0.9381, 0.9381, 2, True),
            ]
        ]
    )
    params = await repo.load_hub_params(FakePool(cursor))
    assert set(params) == {"hub-1", "hub-2", "sub-LZ_AEN-00"}
    assert params["hub-1"].health == "stale"
    assert params["hub-1"].params.p_kw == 11.0
    assert params["hub-1"].params.units == 1
    assert params["hub-2"].params.units == 2  # G-02's per-unit cap is populated, not left None
    assert not params["hub-2"].params.utility_scale
    assert params["sub-LZ_AEN-00"].params.utility_scale  # og.asset SUBSTATION: rated at its nameplate
    sql, sql_params = cursor.executed[0]
    assert "a.asset_id = h.hub_id" in sql
    assert "'SUBSTATION'" in sql and "'MOBILE_STORAGE'" in sql  # core.nameplate: D-31 trucks too
    assert sql_params is None


# ---------------------------------------------------------------------------------------------------
# K12/G-20 clock adapters: recorded `chronyc tracking` outputs and kernel adjtimex states. Every failure
# mode must report +inf (out of limit -> TIMEOUT hold); the old adapter reported 0.0 ms (a PASS).
# ---------------------------------------------------------------------------------------------------

CHRONY_SYNCED = b"""Reference ID    : C0A80101 (ntp1.example.net)
Stratum         : 3
Ref time (UTC)  : Sat Sep 26 09:53:31 2026
System time     : 0.000123456 seconds slow of NTP time
Last offset     : -0.000021370 seconds
RMS offset      : 0.000180522 seconds
Frequency       : 12.914 ppm fast
Residual freq   : -0.001 ppm
Skew            : 0.041 ppm
Root delay      : 0.020233121 seconds
Root dispersion : 0.001204512 seconds
Update interval : 1031.4 seconds
Leap status     : Normal
"""

CHRONY_UNSYNCED = b"""Reference ID    : 00000000 ()
Stratum         : 0
Ref time (UTC)  : Thu Jan 01 00:00:00 1970
System time     : 0.000000000 seconds fast of NTP time
Last offset     : +0.000000000 seconds
RMS offset      : 0.000000000 seconds
Frequency       : 0.000 ppm slow
Residual freq   : +0.000 ppm
Skew            : 0.000 ppm
Root delay      : 1.000000000 seconds
Root dispersion : 1.000000000 seconds
Update interval : 0.0 seconds
Leap status     : Not synchronised
"""


class _FakeChronyProcess:
    def __init__(self, stdout: bytes, *, returncode: int = 0, hang: bool = False) -> None:
        self._stdout = stdout
        self.returncode = returncode
        self._hang = hang
        self.killed = False

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(3600)
        return self._stdout, b""

    def kill(self) -> None:
        self.killed = True


def _patch_chronyc(monkeypatch, process: _FakeChronyProcess | None = None, *, error: Exception | None = None):
    calls: list[tuple] = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        if error is not None:
            raise error
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return calls


async def test_chrony_synced_output_reports_signed_offset(monkeypatch):
    calls = _patch_chronyc(monkeypatch, _FakeChronyProcess(CHRONY_SYNCED))
    offset = await repo.ChronyClockPort().offset_from_ntp_ms()
    assert offset == pytest.approx(-0.123456)  # "slow of NTP time" -> negative
    assert calls[0] == ("chronyc", "tracking")


async def test_chrony_unsynchronised_leap_status_is_out_of_limit(monkeypatch):
    """Regression: a "Not synchronised" chrony still prints a 0.000 s System time, which the old
    adapter reported as a perfect 0.0 ms offset."""
    _patch_chronyc(monkeypatch, _FakeChronyProcess(CHRONY_UNSYNCED))
    assert await repo.ChronyClockPort().offset_from_ntp_ms() == math.inf


async def test_chrony_missing_binary_is_out_of_limit(monkeypatch):
    """Regression (K12 fail-open): the old adapter returned 0.0 ms when chronyc was missing -- which is
    the live host's actual state (it runs ntpd; chronyc is not installed)."""
    _patch_chronyc(monkeypatch, error=FileNotFoundError("chronyc"))
    assert await repo.ChronyClockPort().offset_from_ntp_ms() == math.inf


async def test_chrony_timeout_is_out_of_limit_and_the_process_is_killed(monkeypatch):
    process = _FakeChronyProcess(b"", hang=True)
    _patch_chronyc(monkeypatch, process)
    assert await repo.ChronyClockPort(timeout_s=0.01).offset_from_ntp_ms() == math.inf
    assert process.killed


@pytest.mark.parametrize(
    "stdout",
    [
        b"",
        b"506 Cannot talk to daemon\n",
        b"Reference ID    : ABCD1234\nLeap status     : Normal\n",  # no System time line
        b"System time     : banana seconds fast of NTP time\nLeap status     : Normal\n",
        b"System time     : 0.0001 seconds sideways of NTP time\nLeap status     : Normal\n",
        b"System time     : nan seconds fast of NTP time\nLeap status     : Normal\n",
        b"System time     : 0.0001 seconds fast of NTP time\n",  # no leap status line
    ],
)
async def test_chrony_garbage_output_is_out_of_limit(monkeypatch, stdout):
    _patch_chronyc(monkeypatch, _FakeChronyProcess(stdout))
    assert await repo.ChronyClockPort().offset_from_ntp_ms() == math.inf


async def test_chrony_nonzero_exit_is_out_of_limit(monkeypatch):
    _patch_chronyc(monkeypatch, _FakeChronyProcess(CHRONY_SYNCED, returncode=1))
    assert await repo.ChronyClockPort().offset_from_ntp_ms() == math.inf


async def test_chrony_reading_is_cached_briefly(monkeypatch):
    clock = {"t": 0.0}
    calls = _patch_chronyc(monkeypatch, _FakeChronyProcess(CHRONY_SYNCED))
    port = repo.ChronyClockPort(cache_s=1.0, monotonic_fn=lambda: clock["t"])

    for _ in range(50):  # one tick evaluating 50 batches
        await port.offset_from_ntp_ms()
    assert len(calls) == 1

    clock["t"] = 1.5
    await port.offset_from_ntp_ms()
    assert len(calls) == 2


def _timex(**overrides: int) -> repo.KernelTimex:
    base = {"state": 0, "status": 0x2001, "offset": -145_368, "esterror_us": 1_451, "maxerror_us": 459_704}
    base.update(overrides)
    return repo.KernelTimex(**base)


def test_kernel_offset_from_the_live_hosts_recorded_ntpd_state():
    """Recorded on the live host 2026-09-26 (ntpd, STA_PLL|STA_NANO): -0.145 ms offset, 1.451 ms
    estimated error, 460 ms max error (1024 s poll). Well inside the 200 ms G-20 limit."""
    assert repo.kernel_clock_offset_ms(_timex()) == pytest.approx(-(0.145368 + 1.451))


@pytest.mark.parametrize(
    "overrides",
    [
        {"state": 5},  # TIME_ERROR
        {"status": 0x2041},  # STA_UNSYNC
        {"maxerror_us": 2_500_000},  # daemon stopped updating the kernel
    ],
)
def test_kernel_unsynchronised_states_are_out_of_limit(overrides):
    assert repo.kernel_clock_offset_ms(_timex(**overrides)) == math.inf


def test_kernel_microsecond_offset_units():
    assert repo.kernel_clock_offset_ms(_timex(status=0x0001, offset=2_000, esterror_us=0)) == pytest.approx(
        2.0
    )


async def test_kernel_clock_port_read_failure_is_out_of_limit():
    def unavailable() -> repo.KernelTimex:
        raise OSError("adjtimex is only available on Linux")

    assert await repo.KernelClockPort(read_timex=unavailable).offset_from_ntp_ms() == math.inf


async def test_kernel_clock_port_reports_the_reading():
    port = repo.KernelClockPort(read_timex=lambda: _timex(offset=5_000_000, esterror_us=0))
    assert await port.offset_from_ntp_ms() == pytest.approx(5.0)


def test_build_clock_port_selects_the_configured_source():
    assert isinstance(repo.build_clock_port("kernel"), repo.KernelClockPort)
    assert isinstance(repo.build_clock_port("chrony"), repo.ChronyClockPort)


async def test_build_pg_ports_wires_everything():
    pool = FakePool(FakeCursor([]))
    trace_store = object()  # opaque -- only passed through to TraceStorePort's constructor
    hubs = object()  # GUARD-02/04: caller must supply guardian's own telemetry-backed hubs port
    clock, l2, members = object(), object(), object()
    ports, leases = repo.build_pg_ports(
        pool,
        trace_store,  # type: ignore[arg-type]
        hubs,  # type: ignore[arg-type]
        clock=clock,  # type: ignore[arg-type]
        l2_instructions=l2,  # type: ignore[arg-type]
        bank_members=members,  # type: ignore[arg-type]
        zones_by_bank={"bank-1": "zone-a"},
    )
    assert ports.zones_by_bank == {"bank-1": "zone-a"}
    assert ports.hubs is hubs
    assert ports.clock is clock and ports.l2_instructions is l2 and ports.bank_members is members
    assert isinstance(leases, repo.PgLeaseStatePort)


async def test_load_bank_membership():
    cursor = FakeCursor([[("hub-00000", "bank-000"), ("hub-00040", "bank-000")]])
    assert await repo.load_bank_membership(FakePool(cursor)) == {
        "hub-00000": "bank-000",
        "hub-00040": "bank-000",
    }


async def test_calibration_queue_maps_rows_and_excludes_already_evaluated_attempts():
    calibration_id = uuid4()
    requested_at = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)
    cursor = FakeCursor(
        [
            [
                (
                    calibration_id,
                    "hub-1",
                    Decimal("0.000"),
                    Decimal("60.0000"),
                    Decimal("240.00"),
                    Decimal("-0.0200"),
                    None,
                    Decimal("1.500"),
                    requested_at,
                )
            ]
        ]
    )
    pending = await repo.PgCalibrationQueuePort(FakePool(cursor)).pending(max_age_s=600.0)

    assert pending == [
        repo.PendingCalibration(
            calibration_id=calibration_id,
            hub_id="hub-1",
            reference_phase_deg=0.0,
            reference_freq_hz=60.0,
            reference_amplitude_v=240.0,
            correction=OffsetVector(freq_hz=-0.02, voltage_pct=0.0, phase_deg=1.5),
            requested_at=requested_at,
        )
    ]
    sql, params = cursor.executed[0]
    assert "outcome = 'PENDING'" in sql and "NOT EXISTS" in sql and "og.calibration_command c" in sql
    assert params == {"max_age_s": 600.0, "limit": 20}


async def test_trace_store_port_records_calibration_verdicts_on_the_guardian_stream():
    class RecordingStore:
        def __init__(self) -> None:
            self.appended: list[tuple] = []

        async def append(self, *args):
            self.appended.append(args)

    store = RecordingStore()
    calibration_id = uuid4()
    await repo.TraceStorePort(store).append_calibration_verdict(calibration_id, {"outcome": "SIGNED"})  # type: ignore[arg-type]

    stream, decision_type, event_class, payload = store.appended[0]
    assert (stream, decision_type, event_class) == ("guardian", "GUARDIAN_VERDICT", "GUARDIAN_VERDICT")
    assert payload == {"outcome": "SIGNED", "kind": "CALIBRATION", "calibration_id": str(calibration_id)}


def test_pg_hub_state_port_removed():
    """GUARD-02/04: there is no Postgres-backed HubStatePort in repo.py to default to -- guardian's hub
    read must always be its own MQTT telemetry (opengrid.guardian.mqtt_io.MqttHubStatePort)."""
    assert not hasattr(repo, "PgHubStatePort")


def test_g19_active_obligations_are_scoped_to_the_current_interval_on_this_bank():
    """Regression (live 2026-09-26): every commitment with ANY reservation on the bank (including
    deliveries hours later) was treated as omitted from the batch at 0 kW, so G-19 vetoed every batch.
    Only reservations covering now count, with this bank's reserved kW as the frozen amount."""
    sql = repo._ACTIVE_OBLIGATIONS_FOR_BANK_SQL
    assert "r.interval_start <= now() AND r.interval_end > now()" in sql
    assert "SUM(r.amount)" in sql


async def test_load_zones_by_bank():
    cursor = FakeCursor([[("bank-000", "LZ_NORTH"), ("bank-001", "LZ_SOUTH")]])
    assert await repo.load_zones_by_bank(FakePool(cursor)) == {"bank-000": "LZ_NORTH", "bank-001": "LZ_SOUTH"}


async def test_scope_posture_is_upserted_and_a_stop_is_only_proposed():
    cursor = FakeCursor([None, None])
    pool = FakePool(cursor)
    port = repo.PgScopePosturePort(pool)  # type: ignore[arg-type]
    await port.set_posture(
        "ZONE", "LZ_NORTH", posture="CONSERVATIVE", veto_ratio=0.2, consecutive=3, stop_requested=True
    )
    await port.propose_safe_stop("ZONE", "LZ_NORTH", "3 consecutive CONSERVATIVE ticks")

    upsert_sql, params = cursor.executed[0]
    assert "og.scope_posture" in upsert_sql and "ON CONFLICT (scope_kind, scope_ref)" in upsert_sql
    assert params["posture"] == "CONSERVATIVE" and params["stop_requested"] is True
    propose_sql, propose_params = cursor.executed[1]
    assert "og.operator_action" in propose_sql and "'SAFE_STOP_ENGAGE'" in propose_sql
    assert "confirmed_at" not in propose_sql  # an unconfirmed proposal: a person decides
    assert propose_params["target_ref"] == "ZONE:LZ_NORTH"
    assert "stop_event" not in propose_sql


async def test_alert_port_raises_once_while_open_and_clears_by_condition(monkeypatch):
    import opengrid.health.queries as health_queries

    raised, cleared = [], []

    async def fake_raise(pool, finding, *, opened_at):
        raised.append((finding.rule, finding.detail["condition_key"]))
        return 1

    async def fake_clear(pool, alert_id, *, cleared_at=None):
        cleared.append(alert_id)

    monkeypatch.setattr(health_queries, "raise_alert", fake_raise)
    monkeypatch.setattr(health_queries, "clear_alert", fake_clear)

    already_open = repo.PgAlertPort(FakePool(FakeCursor([[(7,)]])))  # type: ignore[arg-type]
    await already_open.raise_alert("ALR-SCOPE-CONSERVATIVE", "warning", "s", "BANK:bank-000", {})
    fresh = repo.PgAlertPort(FakePool(FakeCursor([[]])))  # type: ignore[arg-type]
    await fresh.raise_alert("ALR-SCOPE-CONSERVATIVE", "warning", "s", "BANK:bank-000", {})
    closing = repo.PgAlertPort(FakePool(FakeCursor([[(7,), (9,)]])))  # type: ignore[arg-type]
    await closing.clear_alert("ALR-SCOPE-CONSERVATIVE", "BANK:bank-000")

    assert raised == [("ALR-SCOPE-CONSERVATIVE", "BANK:bank-000")] and cleared == [7, 9]
