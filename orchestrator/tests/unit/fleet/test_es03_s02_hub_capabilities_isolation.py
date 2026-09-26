"""ES03-S02 (traceability-gap closure, 2026-09-26): `opengrid.fleet.hub_capabilities(bank_id)` must
never leak another bank's hubs into its per-hub eligibility snapshot -- the allocator's per-bank
water-filling cycle (`opengrid.allocator.cycle`) builds its `HubSnapshot` sequence straight from this
call's result, so a cross-bank leak here would let one bank's water-filling see (and potentially
double-count or misattribute) another bank's hub capacity.

Uses the REAL `opengrid.fleet` module against an in-memory `FleetBackend` fake (no DB/MQTT, BUILD.md
S5) -- the same fake shape `test_twin.py` uses, kept local here so this file has no test-to-test
coupling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from opengrid import fleet
from opengrid.core.models.platform import Bank, Hub, HubState
from opengrid.platform.config import Config


@dataclass
class _FakeFleetBackend:
    hubs: list[Hub] = field(default_factory=list)
    banks: list[Bank] = field(default_factory=list)
    states: list[HubState] = field(default_factory=list)

    async def load_hubs(self) -> list[Hub]:
        return self.hubs

    async def load_banks(self) -> list[Bank]:
        return self.banks

    async def load_hub_states(self) -> list[HubState]:
        return self.states

    async def upsert_hub_states(self, states: list[HubState]) -> None:
        return None

    async def copy_telemetry(self, rows) -> None:
        return None

    async def record_scada_observations(self, signals) -> None:
        return None

    async def insert_acks(self, acks) -> None:
        return None


def _cfg() -> Config:
    return Config({"health": {"hub_stale_s": 6.0, "hub_offline_s": 30.0}, "fleet": {}})


def _hub(hub_id: str, bank_id: str, *, p_kw: float = 5.0, e_kwh: float = 39.2) -> Hub:
    return Hub(hub_id=hub_id, bank_id=bank_id, zone="LZ_NORTH", e_kwh=e_kwh, r_kwh=e_kwh * 0.2, p_kw=p_kw)


def _bank(bank_id: str, *, kva_rating: float = 600.0) -> Bank:
    return Bank(bank_id=bank_id, zone="LZ_NORTH", kva_rating=kva_rating)


@pytest.fixture(autouse=True)
async def _reset_fleet_state():
    fleet.configure(_FakeFleetBackend(), _cfg())
    yield
    fleet.configure(_FakeFleetBackend(), _cfg())


async def _seed(backend: _FakeFleetBackend) -> None:
    fleet.configure(backend, _cfg())
    await fleet.load_topology()


async def test_hub_capabilities_never_returns_another_banks_hubs() -> None:
    now = datetime.now(UTC)
    hubs_a = [_hub(f"a{i}", "bank-A") for i in range(3)]
    hubs_b = [_hub(f"b{i}", "bank-B") for i in range(4)]
    states = [HubState(hub_id=h.hub_id, soc_kwh=20.0, p_kw=0.0, last_seen_at=now) for h in (*hubs_a, *hubs_b)]
    backend = _FakeFleetBackend(
        hubs=[*hubs_a, *hubs_b], banks=[_bank("bank-A"), _bank("bank-B")], states=states
    )
    await _seed(backend)

    snapshots_a = fleet.hub_capabilities("bank-A")
    snapshots_b = fleet.hub_capabilities("bank-B")

    assert {s.hub_id for s in snapshots_a} == {"a0", "a1", "a2"}
    assert {s.hub_id for s in snapshots_b} == {"b0", "b1", "b2", "b3"}
    assert all(s.bank_id == "bank-A" for s in snapshots_a)
    assert all(s.bank_id == "bank-B" for s in snapshots_b)
    # No hub from the other bank leaks in either direction.
    assert {s.hub_id for s in snapshots_a}.isdisjoint({s.hub_id for s in snapshots_b})


async def test_hub_capabilities_isolation_holds_after_topology_changes_bank_membership() -> None:
    """A hub reassigned between banks (topology reload) must show up under its NEW bank only -- never
    remembered under its previous one from a stale in-memory mapping."""
    now = datetime.now(UTC)
    hub = _hub("h1", "bank-A")
    backend = _FakeFleetBackend(
        hubs=[hub],
        banks=[_bank("bank-A"), _bank("bank-B")],
        states=[HubState(hub_id="h1", soc_kwh=20.0, p_kw=0.0, last_seen_at=now)],
    )
    await _seed(backend)
    assert {s.hub_id for s in fleet.hub_capabilities("bank-A")} == {"h1"}
    assert fleet.hub_capabilities("bank-B") == []

    # Re-home the hub to bank-B and reload topology (as og-engine does on restart/topology change).
    backend.hubs = [_hub("h1", "bank-B")]
    await fleet.load_topology()

    assert fleet.hub_capabilities("bank-A") == []
    assert {s.hub_id for s in fleet.hub_capabilities("bank-B")} == {"h1"}


async def test_hub_capabilities_unknown_bank_raises_lookup_error() -> None:
    backend = _FakeFleetBackend(hubs=[_hub("h1", "bank-A")], banks=[_bank("bank-A")])
    await _seed(backend)
    with pytest.raises(LookupError):
        fleet.hub_capabilities("bank-unknown")
