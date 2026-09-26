"""ES03/TS-03 fleet-twin unit tests: ingestion, stale/offline transitions (TS-03-04), bank aggregation
(TS-03-05), and the L5/K5 utility-instruction ceiling. No DB/MQTT (BUILD.md S5)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from opengrid import fleet
from opengrid.core.models.mqtt import FLOW_TELEMETRY_FIELDS
from opengrid.core.models.platform import Bank, Hub, HubState
from opengrid.platform.config import Config


@dataclass
class FakeFleetBackend:
    """In-memory fake satisfying `fleet.FleetBackend`, mirroring the trace module's test pattern."""

    hubs: list[Hub] = field(default_factory=list)
    banks: list[Bank] = field(default_factory=list)
    states: list[HubState] = field(default_factory=list)
    upserted: list[HubState] = field(default_factory=list)
    copied_rows: list[fleet.TelemetryRow] = field(default_factory=list)
    recorded_scada: list = field(default_factory=list)
    acks: list = field(default_factory=list)

    async def load_hubs(self) -> list[Hub]:
        return self.hubs

    async def load_banks(self) -> list[Bank]:
        return self.banks

    async def load_hub_states(self) -> list[HubState]:
        return self.states

    async def upsert_hub_states(self, states: list[HubState]) -> None:
        self.upserted.extend(states)

    async def copy_telemetry(self, rows: list[fleet.TelemetryRow]) -> None:
        self.copied_rows.extend(rows)

    async def record_scada_observations(self, signals) -> None:
        self.recorded_scada.extend(signals)

    async def insert_acks(self, acks) -> None:
        self.acks.extend(acks)


def _cfg(**overrides: float) -> Config:
    data = {
        "health": {
            "hub_stale_s": overrides.get("hub_stale_s", 6.0),
            "hub_offline_s": overrides.get("hub_offline_s", 30.0),
        },
        "fleet": {"telemetry_interval_s": overrides.get("telemetry_interval_s", 2.0)},
    }
    return Config(data)


def _hub(hub_id: str, bank_id: str = "bank-1", *, p_kw: float = 5.0, e_kwh: float = 13.5) -> Hub:
    return Hub(hub_id=hub_id, bank_id=bank_id, zone="LZ_NORTH", e_kwh=e_kwh, r_kwh=e_kwh * 0.2, p_kw=p_kw)


def _bank(bank_id: str = "bank-1", *, kva_rating: float = 75.0, reserve_kva: float = 0.0) -> Bank:
    return Bank(bank_id=bank_id, zone="LZ_NORTH", kva_rating=kva_rating, reserve_kva=reserve_kva)


@pytest.fixture(autouse=True)
async def _reset_fleet_state() -> None:
    """`opengrid.fleet` holds module-level twin state; reset it for every test."""
    fleet.configure(FakeFleetBackend(), _cfg())
    yield
    fleet.configure(FakeFleetBackend(), _cfg())


async def _seed(backend: FakeFleetBackend, cfg: Config | None = None) -> None:
    fleet.configure(backend, cfg or _cfg())
    await fleet.load_topology()


# --- ingestion -----------------------------------------------------------------------------------


async def test_ingest_telemetry_updates_in_memory_state_and_buffers_a_row() -> None:
    backend = FakeFleetBackend(hubs=[_hub("h1")], banks=[_bank()])
    await _seed(backend)

    await fleet.ingest_telemetry(
        {
            "hub_id": "h1",
            "bank_id": "bank-1",
            "zone": "LZ_NORTH",
            "ts": datetime.now(UTC).isoformat(),
            "soc_kwh": 10.0,
            "p_kw": -2.0,
            "health": "online",
            "seq": 1,
            "epoch": 1,
        }
    )

    assert fleet.hub_health("h1") == "online"
    stats = await fleet.flush()
    assert stats.telemetry_rows == 1
    assert stats.hub_states == 1
    assert backend.copied_rows[0].hub_id == "h1"


async def test_discharge_flow_telemetry_reaches_the_snapshot_hub_state_and_telemetry_row() -> None:
    """Migration 0027 wiring: the optional flow fields (BMS limits, cell temperature, meter/home/PV) are
    carried to `hub_capabilities` (dispatch derating), the persisted hub_state and the telemetry row;
    a hub on the old wire schema leaves them None."""
    backend = FakeFleetBackend(hubs=[_hub("h1"), _hub("h2")], banks=[_bank()])
    await _seed(backend)
    base = {"bank_id": "bank-1", "zone": "LZ_NORTH", "soc_kwh": 10.0, "p_kw": -2.0, "health": "online"}
    now = datetime.now(UTC).isoformat()
    await fleet.ingest_telemetry(
        {
            **base,
            "hub_id": "h1",
            "ts": now,
            "seq": 1,
            "epoch": 1,
            "cell_temp_c": 41.5,
            "p_dis_max_kw": 7.0,
            "meter_kw": 1.2,
            "home_load_kw": 3.4,
            "pv_kw": 0.0,
            "p_ch_max_kw": 6.0,
            "peak_power_budget_kws": 900.0,
            "charge_pv_kw": 2.5,
            "charge_grid_kw": 1.5,
            "lat": 32.7,
            "lon": -96.9,
        }
    )
    await fleet.ingest_telemetry({**base, "hub_id": "h2", "ts": now, "seq": 1, "epoch": 1})

    snaps = {s.hub_id: s for s in fleet.hub_capabilities("bank-1")}
    assert snaps["h1"].cell_temp_c == 41.5 and snaps["h1"].p_dis_max_kw == 7.0
    assert snaps["h2"].cell_temp_c is None and snaps["h2"].p_dis_max_kw is None

    await fleet.flush()
    states = {s.hub_id: s for s in backend.upserted}
    assert states["h1"].meter_kw == 1.2 and states["h1"].peak_power_budget_kws == 900.0
    assert states["h2"].meter_kw is None
    row = next(r for r in backend.copied_rows if r.hub_id == "h1")
    persisted = dict(zip(FLOW_TELEMETRY_FIELDS, row.flow, strict=True))
    assert persisted["home_load_kw"] == 3.4
    # D-28 charge-source split (migration 0034) reaches og.telemetry and og.hub_state; lat/lon are accepted
    # on the wire but not persisted per sample (og.hub holds the position).
    assert persisted["charge_pv_kw"] == 2.5 and persisted["charge_grid_kw"] == 1.5
    assert "lat" not in persisted
    assert states["h1"].charge_pv_kw == 2.5 and states["h1"].charge_grid_kw == 1.5
    assert states["h2"].charge_pv_kw is None


async def test_a_substation_hub_carries_utility_scale_into_params_and_snapshot() -> None:
    """REVIEW-FIX (D-29): without utility_scale the 20 MW toll set would be capped at the 11 kW home unit."""
    toll = Hub(
        hub_id="sub-1",
        bank_id="bank-1",
        zone="LZ_AEN",
        e_kwh=40000.0,
        r_kwh=8000.0,
        p_kw=20000.0,
        utility_scale=True,
    )
    backend = FakeFleetBackend(hubs=[toll, _hub("h1")], banks=[_bank()])
    await _seed(backend)
    now = datetime.now(UTC)
    base = {"bank_id": "bank-1", "zone": "LZ_AEN", "soc_kwh": 20000.0, "p_kw": 0.0, "health": "online"}
    await fleet.ingest_telemetry({**base, "hub_id": "sub-1", "ts": now, "seq": 1, "epoch": 1})
    await fleet.ingest_telemetry({**base, "hub_id": "h1", "ts": now, "seq": 1, "epoch": 1, "soc_kwh": 5.0})
    snaps = {s.hub_id: s for s in fleet.hub_capabilities("bank-1")}
    assert snaps["sub-1"].utility_scale is True
    assert snaps["h1"].utility_scale is False


async def test_two_scada_signals_for_one_bank_in_one_tick_are_both_kept_and_flushed() -> None:
    """R3 (FLEET-SIM / GUARDIAN-FLOW): APPARENT_POWER_KVA and the signed REAL_POWER_KW for the same bank in
    one tick must both reach og.feed_obs; keyed per bank only, the second clobbered the first."""
    backend = FakeFleetBackend(hubs=[_hub("h1")], banks=[_bank()])
    await _seed(backend)
    ts = datetime.now(UTC)
    kva = {
        "bank_id": "bank-1",
        "signal": "APPARENT_POWER_KVA",
        "value": 420.0,
        "unit": "kVA",
        "quality": "good",
    }
    kw = {"bank_id": "bank-1", "signal": "REAL_POWER_KW", "value": -150.0, "unit": "kW", "quality": "good"}
    await fleet.ingest_scada_signal({**kva, "ts": ts})
    await fleet.ingest_scada_signal({**kw, "ts": ts + timedelta(milliseconds=5)})

    kva_signal = fleet.bank_scada_signal("bank-1", "APPARENT_POWER_KVA")
    assert kva_signal is not None and kva_signal.value == 420.0
    real_signal = fleet.bank_scada_signal("bank-1", "REAL_POWER_KW")
    assert real_signal is not None and real_signal.value == -150.0
    newest = fleet.bank_scada_signal("bank-1")  # no signal given: the newest reading of any signal
    assert newest is not None and newest.signal == "REAL_POWER_KW"

    await fleet.flush()
    assert sorted(s.signal for s in backend.recorded_scada) == ["APPARENT_POWER_KVA", "REAL_POWER_KW"]


async def test_bank_feeder_reports_the_banks_feeder_or_none() -> None:
    backend = FakeFleetBackend(
        hubs=[],
        banks=[
            Bank(bank_id="bank-1", zone="LZ_NORTH", kva_rating=600.0, feeder_id="feeder-LZ_NORTH-00"),
            _bank("bank-2"),
        ],
    )
    await _seed(backend)
    assert fleet.bank_feeder("bank-1") == "feeder-LZ_NORTH-00"
    assert fleet.bank_feeder("bank-2") is None
    with pytest.raises(LookupError):
        fleet.bank_feeder("bank-9")


async def test_ingest_telemetry_for_unknown_hub_is_logged_and_skipped() -> None:
    backend = FakeFleetBackend(hubs=[], banks=[])
    await _seed(backend)

    await fleet.ingest_telemetry(
        {
            "hub_id": "ghost",
            "bank_id": "bank-1",
            "zone": "LZ_NORTH",
            "ts": datetime.now(UTC).isoformat(),
            "soc_kwh": 10.0,
            "p_kw": 0.0,
            "health": "online",
            "seq": 1,
            "epoch": 1,
        }
    )
    stats = await fleet.flush()
    assert stats.telemetry_rows == 0  # never buffered -- hub is unknown


# --- TS-03-04: stale/offline exclusion from capability() -----------------------------------------


async def test_capability_excludes_hub_with_no_telemetry_ever() -> None:
    backend = FakeFleetBackend(hubs=[_hub("h1")], banks=[_bank()])
    await _seed(backend)

    cap = await fleet.capability("bank-1", datetime.now(UTC))
    assert "h1" in cap.excluded_hub_ids
    assert cap.max_discharge_kw == 0.0


async def test_capability_excludes_stale_and_offline_hubs_but_keeps_online_ones() -> None:
    now = datetime.now(UTC)
    backend = FakeFleetBackend(
        hubs=[_hub("online"), _hub("stale"), _hub("offline")],
        banks=[_bank(kva_rating=1000.0)],
        states=[
            HubState(hub_id="online", soc_kwh=10.0, p_kw=0.0, last_seen_at=now),
            HubState(hub_id="stale", soc_kwh=10.0, p_kw=0.0, last_seen_at=now - timedelta(seconds=10)),
            HubState(hub_id="offline", soc_kwh=10.0, p_kw=0.0, last_seen_at=now - timedelta(seconds=60)),
        ],
    )
    await _seed(backend)

    cap = await fleet.capability("bank-1", now)
    assert cap.excluded_hub_ids == {"stale", "offline"}
    assert cap.max_discharge_kw > 0.0  # the online hub's capability still counts


async def test_hub_health_classifies_fault_independent_of_timing() -> None:
    now = datetime.now(UTC)
    backend = FakeFleetBackend(
        hubs=[_hub("faulty")],
        banks=[_bank()],
        states=[HubState(hub_id="faulty", soc_kwh=10.0, p_kw=0.0, last_seen_at=now, fault_code="INV_TRIP")],
    )
    await _seed(backend)
    assert fleet.hub_health("faulty", now=now) == "fault"


async def test_flush_reclassifies_hubs_that_stopped_publishing_without_new_telemetry() -> None:
    """TS-03-04: exclusion must be detected by the periodic pass, not only on the next telemetry
    message (a hub that goes silent never sends one)."""
    now = datetime.now(UTC)
    backend = FakeFleetBackend(
        hubs=[_hub("h1")],
        banks=[_bank()],
        states=[HubState(hub_id="h1", soc_kwh=10.0, p_kw=0.0, last_seen_at=now)],
    )
    await _seed(backend)
    assert fleet.hub_health("h1", now=now) == "online"

    later = now + timedelta(seconds=45)
    assert fleet.hub_health("h1", now=later) == "offline"
    await fleet.flush(now=later)
    assert backend.upserted[-1].health == "offline"  # persisted with the full health vocabulary


async def test_unknown_bank_raises_lookup_error() -> None:
    with pytest.raises(LookupError):
        await fleet.capability("nope", datetime.now(UTC))


# --- TS-03-05: bank aggregation matches hub sum ---------------------------------------------------


async def test_bank_capability_sums_member_hub_discharge_within_kva_rating() -> None:
    now = datetime.now(UTC)
    hubs = [_hub(f"h{i}", p_kw=5.0) for i in range(10)]
    states = [HubState(hub_id=h.hub_id, soc_kwh=10.0, p_kw=0.0, last_seen_at=now) for h in hubs]
    backend = FakeFleetBackend(hubs=hubs, banks=[_bank(kva_rating=1000.0)], states=states)
    await _seed(backend)

    cap = await fleet.capability("bank-1", now)
    assert cap.max_discharge_kw == pytest.approx(50.0)  # 10 hubs * 5 kW, well under the 1000 kVA cap


async def test_bank_capability_is_capped_by_kva_rating_net_of_reserve() -> None:
    now = datetime.now(UTC)
    hubs = [_hub(f"h{i}", p_kw=5.0) for i in range(10)]
    states = [HubState(hub_id=h.hub_id, soc_kwh=10.0, p_kw=0.0, last_seen_at=now) for h in hubs]
    backend = FakeFleetBackend(hubs=hubs, banks=[_bank(kva_rating=30.0, reserve_kva=5.0)], states=states)
    await _seed(backend)

    cap = await fleet.capability("bank-1", now)
    assert cap.max_discharge_kw == pytest.approx(25.0)  # 50 kW summed but capped at 30-5


async def test_hub_below_reserve_floor_contributes_zero_discharge() -> None:
    now = datetime.now(UTC)
    hub = _hub("h1", e_kwh=13.5, p_kw=5.0)  # r_kwh = 2.7
    backend = FakeFleetBackend(
        hubs=[hub],
        banks=[_bank(kva_rating=1000.0)],
        states=[HubState(hub_id="h1", soc_kwh=1.0, p_kw=0.0, last_seen_at=now)],  # below reserve
    )
    await _seed(backend)
    cap = await fleet.capability("bank-1", now)
    assert cap.max_discharge_kw == 0.0
    assert cap.max_charge_kw > 0.0  # plenty of headroom to charge


# --- SCADA / utility instruction ------------------------------------------------------------------


async def test_ingest_scada_signal_bounds_charge_headroom() -> None:
    now = datetime.now(UTC)
    hub = _hub("h1", p_kw=100.0)
    backend = FakeFleetBackend(
        hubs=[hub],
        banks=[_bank(kva_rating=50.0, reserve_kva=0.0)],
        states=[HubState(hub_id="h1", soc_kwh=5.0, p_kw=0.0, last_seen_at=now)],
    )
    await _seed(backend)

    await fleet.ingest_scada_signal(
        {
            "bank_id": "bank-1",
            "signal": "APPARENT_POWER_KVA",
            "value": 40.0,
            "unit": "kVA",
            "quality": "good",
            "ts": now.isoformat(),
        }
    )
    cap = await fleet.capability("bank-1", now)
    assert cap.max_charge_kw == pytest.approx(10.0)  # 50 kVA rating - 40 kVA load
    # Persisted to og.feed_obs (guardian/health read it from other processes) -- but on flush, never
    # inline on the MQTT ingest path.
    assert backend.recorded_scada == []
    await fleet.flush(now=now)
    assert [s.bank_id for s in backend.recorded_scada] == ["bank-1"]


async def test_scada_ingest_never_touches_the_database_and_flush_writes_latest_per_bank() -> None:
    """Regression (live 2026-09-26): a per-message INSERT+COMMIT for every SCADA reading on the MQTT
    ingest path fell behind under WAL pressure; aiomqtt queued the backlog in memory and the twin
    processed telemetry minutes late, so all 2,000 hubs went stale/offline and dispatch stopped."""
    now = datetime.now(UTC)
    backend = FakeFleetBackend(hubs=[_hub("h1")], banks=[_bank(), _bank("bank-2")])
    await _seed(backend)

    for i, bank_id in enumerate(("bank-1", "bank-1", "bank-2")):
        await fleet.ingest_scada_signal(
            {
                "bank_id": bank_id,
                "signal": "APPARENT_POWER_KVA",
                "value": 10.0 + i,
                "unit": "kVA",
                "quality": "good",
                "ts": (now + timedelta(seconds=i)).isoformat(),
            }
        )
    assert backend.recorded_scada == []

    await fleet.flush(now=now)
    assert sorted((s.bank_id, s.value) for s in backend.recorded_scada) == [
        ("bank-1", 11.0),
        ("bank-2", 12.0),
    ]

    await fleet.flush(now=now)  # nothing new buffered -> nothing rewritten
    assert len(backend.recorded_scada) == 2


async def test_utility_block_instruction_zeroes_capability() -> None:
    now = datetime.now(UTC)
    hub = _hub("h1", p_kw=10.0)
    backend = FakeFleetBackend(
        hubs=[hub],
        banks=[_bank(kva_rating=1000.0)],
        states=[HubState(hub_id="h1", soc_kwh=10.0, p_kw=0.0, last_seen_at=now)],
    )
    await _seed(backend)

    await fleet.ingest_utility_instruction(
        {
            "instruction_id": "00000000-0000-7000-8000-000000000001",
            "bank_id": "bank-1",
            "kind": "BLOCK",
            "issued_at": now.isoformat(),
            "issued_by": "utility",
        }
    )
    cap = await fleet.capability("bank-1", now)
    assert cap.max_discharge_kw == 0.0
    assert cap.max_charge_kw == 0.0


async def test_utility_limit_instruction_caps_discharge() -> None:
    now = datetime.now(UTC)
    hubs = [_hub(f"h{i}", p_kw=10.0) for i in range(5)]
    states = [HubState(hub_id=h.hub_id, soc_kwh=10.0, p_kw=0.0, last_seen_at=now) for h in hubs]
    backend = FakeFleetBackend(hubs=hubs, banks=[_bank(kva_rating=1000.0)], states=states)
    await _seed(backend)

    await fleet.ingest_utility_instruction(
        {
            "instruction_id": "00000000-0000-7000-8000-000000000002",
            "bank_id": "bank-1",
            "kind": "LIMIT",
            "limit_kw": 12.0,
            "issued_at": now.isoformat(),
            "issued_by": "utility",
        }
    )
    cap = await fleet.capability("bank-1", now)
    assert cap.max_discharge_kw == pytest.approx(12.0)  # would otherwise be 50 kW


async def test_expired_utility_instruction_no_longer_applies() -> None:
    now = datetime.now(UTC)
    hub = _hub("h1", p_kw=10.0)
    backend = FakeFleetBackend(
        hubs=[hub],
        banks=[_bank(kva_rating=1000.0)],
        states=[HubState(hub_id="h1", soc_kwh=10.0, p_kw=0.0, last_seen_at=now)],
    )
    await _seed(backend)

    await fleet.ingest_utility_instruction(
        {
            "instruction_id": "00000000-0000-7000-8000-000000000003",
            "bank_id": "bank-1",
            "kind": "BLOCK",
            "issued_at": (now - timedelta(minutes=10)).isoformat(),
            "expires_at": (now - timedelta(minutes=5)).isoformat(),
            "issued_by": "utility",
        }
    )
    cap = await fleet.capability("bank-1", now)
    assert cap.max_discharge_kw > 0.0


async def test_ogsim_lift_message_unblocks_a_previously_blocked_bank() -> None:
    """Read-only regression for the ogsim wave-2/blocker-3 fix (`ogsim.scada.anomalies`/
    `runtime.py`): ending a `utility_instruction` anomaly now republishes the SAME kind with
    `expires_at` set to its own `issued_at` (already in the past on arrival) instead of the
    old hardcoded `None` ("never expires"). This feeds that EXACT message shape --
    `ScadaEngine._instruction_message`'s output for `pending["lift"] = True`, reproduced here
    without importing `ogsim` (BUILD.md S1: opengrid and ogsim share only `interfaces/`) --
    into `fleet.ingest_utility_instruction` and confirms `fleet.capability`
    (`_active_utility_limit_kw`'s consumer) reports the bank unblocked afterward, exactly as
    it already does for the hand-crafted `test_expired_utility_instruction_no_longer_applies`
    case above -- this test additionally asserts the BLOCK was actually active first, so it
    proves a lift, not merely that a pre-expired instruction is ignorable."""
    now = datetime.now(UTC)
    hub = _hub("h1", p_kw=10.0)
    backend = FakeFleetBackend(
        hubs=[hub],
        banks=[_bank(kva_rating=1000.0)],
        states=[HubState(hub_id="h1", soc_kwh=10.0, p_kw=0.0, last_seen_at=now)],
    )
    await _seed(backend)

    # 1. The anomaly starts: ogsim publishes a BLOCK with expires_at=None ("never expires" --
    #    ogsim.scada.runtime.ScadaEngine._instruction_message's shape before a lift).
    await fleet.ingest_utility_instruction(
        {
            "instruction_id": "00000000-0000-7000-8000-000000000004",
            "bank_id": "bank-1",
            "kind": "BLOCK",
            "issued_at": (now - timedelta(seconds=5)).isoformat(),
            "expires_at": None,
            "issued_by": "SCENARIO_ANOMALY",
        }
    )
    blocked = await fleet.capability("bank-1", now)
    assert blocked.max_discharge_kw == 0.0
    assert blocked.max_charge_kw == 0.0

    # 2. The anomaly ends (natural expiry or manual cancel): ogsim's fix republishes the SAME
    #    kind with expires_at == issued_at (already past) -- the "lift" message shape. Backed
    #    off by 1s from `now` (not exactly `now`) so the comparison is robust regardless of
    #    how much wall-clock time elapses between here and fleet.capability()'s own
    #    datetime.now(UTC) call below.
    lift_issued_at = (now - timedelta(seconds=1)).isoformat()
    await fleet.ingest_utility_instruction(
        {
            "instruction_id": "00000000-0000-7000-8000-000000000005",
            "bank_id": "bank-1",
            "kind": "BLOCK",
            "limit_kw": None,
            "issued_at": lift_issued_at,
            "expires_at": lift_issued_at,
            "issued_by": "SCENARIO_ANOMALY",
        }
    )

    unblocked = await fleet.capability("bank-1", now)
    assert unblocked.max_discharge_kw > 0.0
    assert unblocked.max_charge_kw > 0.0


# --- restart recovery ------------------------------------------------------------------------------


async def test_load_topology_rehydrates_from_persisted_hub_state() -> None:
    now = datetime.now(UTC)
    backend = FakeFleetBackend(
        hubs=[_hub("h1")],
        banks=[_bank()],
        states=[HubState(hub_id="h1", soc_kwh=9.0, p_kw=-1.5, last_seen_at=now)],
    )
    await _seed(backend)
    cap = await fleet.capability("bank-1", now)
    assert "h1" not in cap.excluded_hub_ids


async def test_a3_hub_acks_are_buffered_and_persisted_on_flush() -> None:
    """A3: hub acknowledgements were never persisted (live 2026-09-26: visible only on MQTT). An
    accepted ack sets the hub's last_command_id; every ack (accepted or rejected) is written on flush."""
    now = datetime.now(UTC)
    backend = FakeFleetBackend(
        hubs=[_hub("h1")],
        banks=[_bank()],
        states=[HubState(hub_id="h1", soc_kwh=5.0, p_kw=0.0, last_seen_at=now)],
    )
    await _seed(backend)
    accepted_batch = "5d3f0d8c-f0d4-4191-bfbc-eb94d834c621"

    await fleet.ingest_ack(
        {
            "hub_id": "h1",
            "batch_id": accepted_batch,
            "accepted": True,
            "applied_p_kw": -3.0,
            "ts": now.isoformat(),
        }
    )
    await fleet.ingest_ack(
        {
            "hub_id": "h1",
            "batch_id": "6d3f0d8c-f0d4-4191-bfbc-eb94d834c621",
            "accepted": False,
            "reject_reason": "STALE_SEQ",
            "ts": now.isoformat(),
        }
    )
    assert backend.acks == []

    await fleet.flush(now=now)

    assert [(str(a.batch_id), a.accepted, a.reject_reason) for a in backend.acks] == [
        (accepted_batch, True, None),
        ("6d3f0d8c-f0d4-4191-bfbc-eb94d834c621", False, "STALE_SEQ"),
    ]
    assert str(backend.upserted[-1].last_command_id) == accepted_batch


async def test_rated_discharge_is_structural_not_live() -> None:
    """The admission-reject check needs what a bank could EVER deliver: every hub at rated power, capped
    by the bank's kVA rating -- independent of health/SoC (a stale hub still counts)."""
    backend = FakeFleetBackend(
        hubs=[_hub("h1", p_kw=11.0), _hub("h2", p_kw=20.0)], banks=[_bank(kva_rating=600.0)], states=[]
    )
    await _seed(backend)

    assert fleet.rated_discharge_kw("bank-1") == pytest.approx(31.0)
    with pytest.raises(LookupError):
        fleet.rated_discharge_kw("bank-unknown")


async def test_a_failed_telemetry_write_requeues_the_rows(monkeypatch) -> None:
    """Review #13: flush took the buffer before writing and dropped it on a database error, losing
    telemetry (K1/K3 evidence). The rows now go back ahead of newer ones and are written next time."""
    backend = FakeFleetBackend(hubs=[_hub("h1")], banks=[_bank()])
    await _seed(backend)

    async def _telemetry(seq: int) -> None:
        await fleet.ingest_telemetry(
            {
                "hub_id": "h1",
                "bank_id": "bank-1",
                "zone": "LZ_NORTH",
                "ts": datetime.now(UTC).isoformat(),
                "soc_kwh": 10.0,
                "p_kw": -2.0,
                "health": "online",
                "seq": seq,
                "epoch": 1,
            }
        )

    await _telemetry(1)
    original = backend.copy_telemetry

    async def _fail(rows):
        raise RuntimeError("disk stall")

    backend.copy_telemetry = _fail  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await fleet.flush()
    await _telemetry(2)
    backend.copy_telemetry = original  # type: ignore[method-assign]
    await fleet.flush()

    assert [r.seq for r in backend.copied_rows] == [1, 2]


def test_requeue_is_bounded_and_drops_the_oldest(monkeypatch) -> None:
    monkeypatch.setattr(fleet, "REQUEUE_MAX_ROWS", 3)
    buffer = [4, 5]
    fleet._requeue(buffer, [1, 2, 3], "telemetry")
    assert buffer == [3, 4, 5]
