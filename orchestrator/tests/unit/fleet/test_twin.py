"""ES03/TS-03 fleet-twin unit tests: ingestion, stale/offline transitions (TS-03-04), bank aggregation
(TS-03-05), and the L5/K5 utility-instruction ceiling. No DB/MQTT (BUILD.md S5)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from opengrid import fleet
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
