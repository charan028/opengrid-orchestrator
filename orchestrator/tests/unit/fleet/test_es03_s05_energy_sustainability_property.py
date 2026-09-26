"""ES03-S05 (traceability-gap closure, 2026-09-26): a property test on the REAL fleet twin
(`opengrid.fleet.capability`, in-memory `FleetBackend` fake, no DB/MQTT per BUILD.md S5) for the K1
energy-sufficiency invariant (00-invariants.md, BUILD.md S2a.1): the discharge power `fleet` reports
available for a FULL interval must never demand more energy than the hub actually holds above its
reserve floor for that interval.

`opengrid.core.physics.hub_sustainable_discharge_kw` is the correct, already-shared formula for this
(`allocator`'s capability path uses it, per that function's own docstring); the independent re-check
below computes it directly from the hub's raw SoC/reserve/rating and compares it against what
`fleet.capability()` actually reports.

**Fixed 2026-09-26 (architect item (d)); the strict xfail is removed.** It failed while
`opengrid.core.physics.hub_capability` (called by
`fleet.capability()`/`fleet.hub_capabilities()`) returns the hub's full rated `p_kw` whenever
`soc_kwh > reserve_kwh` by ANY amount, never de-rating for how little energy is actually left above
reserve or how long the interval is -- see `hub_capability`'s body (`orchestrator/src/opengrid/core/
physics.py`, the `max_discharge_kw = params.p_kw if usable_above_reserve > 0 else 0.0` line) vs.
`hub_sustainable_discharge_kw`'s energy-limited formula two functions above it in the same file. A hub
sitting at (say) 0.1 kWh above its reserve floor is reported fully capable of its rated 11 kW discharge
for a whole 15-minute interval, which would draw ~2.75 kWh -- 27x more than the 0.1 kWh actually
available. The live-path agent owns the fix (BUILD.md task brief); this test's `xfail` is removed once
`fleet`/`hub_capability` switches to the energy-limited formula.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from hypothesis import given, settings
from hypothesis import strategies as st

from opengrid import fleet
from opengrid.core.models.platform import Bank, Hub, HubState
from opengrid.core.physics import DEFAULT_ETA_D, hub_sustainable_discharge_kw
from opengrid.platform.config import Config

_TOL_KW = 1e-6
_INTERVAL_HOURS = 0.25  # 02a S3: the selector's 15-minute interval -- a "full interval" for K1


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


async def _capability_for_single_hub(e_kwh: float, reserve_frac: float, p_kw: float, soc_kwh: float):
    now = datetime.now(UTC)
    reserve_kwh = e_kwh * reserve_frac
    hub = Hub(hub_id="h1", bank_id="bank-1", zone="LZ_NORTH", e_kwh=e_kwh, r_kwh=reserve_kwh, p_kw=p_kw)
    bank = Bank(bank_id="bank-1", zone="LZ_NORTH", kva_rating=max(p_kw * 2, 1000.0))
    backend = _FakeFleetBackend(
        hubs=[hub],
        banks=[bank],
        states=[HubState(hub_id="h1", soc_kwh=soc_kwh, p_kw=0.0, last_seen_at=now)],
    )
    fleet.configure(backend, _cfg())
    await fleet.load_topology()
    cap = await fleet.capability("bank-1", now)
    return cap, reserve_kwh


@given(
    e_kwh=st.floats(min_value=5.0, max_value=80.0, allow_nan=False, allow_infinity=False),
    reserve_frac=st.floats(min_value=0.1, max_value=0.5, allow_nan=False, allow_infinity=False),
    p_kw=st.floats(min_value=1.0, max_value=20.0, allow_nan=False, allow_infinity=False),
    soc_above_reserve_kwh=st.floats(min_value=0.001, max_value=2.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=200, deadline=None)
def test_reported_capability_never_exceeds_soc_derived_sustainable_power(
    e_kwh: float, reserve_frac: float, p_kw: float, soc_above_reserve_kwh: float
) -> None:
    import asyncio

    reserve_kwh = e_kwh * reserve_frac
    soc_kwh = min(reserve_kwh + soc_above_reserve_kwh, e_kwh)  # never above the hub's own energy cap

    cap, reserve_kwh = asyncio.run(_capability_for_single_hub(e_kwh, reserve_frac, p_kw, soc_kwh))

    # K1: the energy `fleet` reports as sustainable discharge power for a FULL interval, PLUS whatever
    # is already reserved for other committed obligations (0 here -- this test isolates the twin's own
    # report from the ledger's separate K2 accounting), must never exceed what a full interval of
    # discharge at that power would actually draw from the energy held above reserve.
    active_reservations_kw = 0.0
    reported_plus_reserved_kw = cap.max_discharge_kw + active_reservations_kw

    sustainable_kw = hub_sustainable_discharge_kw(soc_kwh, reserve_kwh, p_kw, _INTERVAL_HOURS, DEFAULT_ETA_D)
    assert reported_plus_reserved_kw <= sustainable_kw + _TOL_KW, (
        f"fleet reported {reported_plus_reserved_kw:.6f} kW sustainable, but only {sustainable_kw:.6f} kW "
        f"is actually deliverable for a full {_INTERVAL_HOURS}h interval from "
        f"{soc_kwh - reserve_kwh:.6f} kWh above reserve"
    )
