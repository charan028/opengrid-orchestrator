"""`gate.load_banks`'s live energy envelope (user requirement, live diagnosis 2026-09-25/26): "the
selector must use the LIVE initial SoC per bank from the twin". Regression coverage for the
`INFEASIBLE_F1` risk found while wiring this: an offline/stale hub must never contribute a stale/guessed
SoC, and a bank with zero currently-online hubs must fall back to "no energy envelope this cycle"
(`BankSnapshot.capacity_kwh <= 0`) rather than pinning `soc == 0` against `[reserve_kwh, capacity_kwh]`
bounds computed from a larger, not-currently-live hub set.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from opengrid.core.physics import DEFAULT_ETA_C
from opengrid.fleet import AvailableCapability, HubCapabilitySnapshot
from opengrid.selector import gate

HORIZON_START = datetime(2026, 9, 26, 0, 0, tzinfo=UTC)
HORIZON_END = HORIZON_START + timedelta(minutes=15)


def _capability(bank_id: str, max_discharge_kw: float) -> AvailableCapability:
    return AvailableCapability(
        bank_id=bank_id,
        interval_start=HORIZON_START,
        max_discharge_kw=max_discharge_kw,
        max_charge_kw=0.0,
        excluded_hub_ids=frozenset(),
    )


async def test_load_banks_sums_live_soc_capacity_reserve_from_online_hubs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two online hubs (39.2 kWh/7.84 kWh reserve each, the live 2026-09-25 hardware values) and one
    offline hub on the same bank: the offline hub's (unknown) SoC must not be summed in."""

    async def _fake_capability(bank_id, interval_start):
        return _capability(bank_id, 22.0)

    def _fake_hub_capabilities(bank_id):
        return [
            HubCapabilitySnapshot(
                hub_id="hub-1",
                bank_id=bank_id,
                free_discharge_kw=11.0,
                health="online",
                last_seen_at=HORIZON_START,
                soc_kwh=20.0,
                reserve_kwh=7.84,
                e_kwh=39.2,
                eta_d=0.9487,
            ),
            HubCapabilitySnapshot(
                hub_id="hub-2",
                bank_id=bank_id,
                free_discharge_kw=11.0,
                health="online",
                last_seen_at=HORIZON_START,
                soc_kwh=15.0,
                reserve_kwh=7.84,
                e_kwh=39.2,
                eta_d=0.9487,
            ),
            HubCapabilitySnapshot(
                hub_id="hub-3",
                bank_id=bank_id,
                free_discharge_kw=0.0,
                health="offline",
                last_seen_at=None,
                soc_kwh=None,
                reserve_kwh=None,
                e_kwh=None,
            ),
        ]

    monkeypatch.setattr(gate, "fleet_capability", _fake_capability)
    monkeypatch.setattr(gate, "fleet_hub_capabilities", _fake_hub_capabilities)

    banks = await gate.load_banks(HORIZON_START, HORIZON_END, ("bank-000",))

    assert len(banks) == 1
    bank = banks[0]
    assert bank.models_soc
    assert bank.capacity_kwh == pytest.approx(78.4)  # 2 online hubs x 39.2 kWh
    assert bank.reserve_kwh == pytest.approx(15.68)  # 2 online hubs x 7.84 kWh
    assert bank.initial_soc_kwh == pytest.approx(35.0)  # 20 + 15, offline hub excluded
    assert bank.eta_c == DEFAULT_ETA_C
    assert bank.eta_d == pytest.approx(0.9487)


async def test_load_banks_skips_soc_modeling_when_every_hub_is_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: during the live 2026-09-25/26 `og-engine` restart loop every hub on every bank went
    "offline". Before this fix, `BankSnapshot.initial_soc_kwh`/`capacity_kwh`/`reserve_kwh` were never
    populated at all (always 0.0 defaults) so this case was accidentally safe; after wiring live SoC in,
    a bank with zero online hubs must still degrade to "no energy envelope" rather than the SoC
    variable's own `[reserve_kwh, capacity_kwh]` bounds becoming infeasible against an initial_soc_kwh
    computed from a different hub set."""

    async def _fake_capability(bank_id, interval_start):
        return _capability(bank_id, 0.0)

    def _fake_hub_capabilities(bank_id):
        return [
            HubCapabilitySnapshot(
                hub_id="hub-1",
                bank_id=bank_id,
                free_discharge_kw=0.0,
                health="offline",
                last_seen_at=None,
                soc_kwh=None,
                reserve_kwh=None,
                e_kwh=None,
            )
        ]

    monkeypatch.setattr(gate, "fleet_capability", _fake_capability)
    monkeypatch.setattr(gate, "fleet_hub_capabilities", _fake_hub_capabilities)

    banks = await gate.load_banks(HORIZON_START, HORIZON_END, ("bank-000",))

    assert len(banks) == 1
    assert not banks[0].models_soc
    assert banks[0].capacity_kwh == 0.0
    assert banks[0].reserve_kwh == 0.0
    assert banks[0].initial_soc_kwh == 0.0


async def test_load_banks_passes_the_live_charge_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression (live 2026-09-26): `max_charge_kw` was never populated, so the LP could never plan a
    recharge and every gate's C15 terminal floor was infeasible."""

    async def _fake_capability(bank_id, interval_start):
        return AvailableCapability(
            bank_id=bank_id,
            interval_start=interval_start,
            max_discharge_kw=22.0,
            max_charge_kw=9.5,
            excluded_hub_ids=frozenset(),
        )

    monkeypatch.setattr(gate, "fleet_capability", _fake_capability)
    monkeypatch.setattr(gate, "fleet_hub_capabilities", lambda bank_id: [])

    (bank,) = await gate.load_banks(HORIZON_START, HORIZON_END, ("bank-000",))

    assert bank.max_charge_kw == {0: 9.5}
