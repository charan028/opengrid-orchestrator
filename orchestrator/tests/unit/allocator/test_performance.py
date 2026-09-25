"""BUILD.md S5 performance target: < 200 ms per cycle for 2,000 hubs / 40 banks; 10,000 hubs also
measured (not required to pass under 200 ms, but reported so a regression is visible)."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from opengrid.allocator.cycle import cycle
from opengrid.allocator.models import (
    BankSnapshot,
    FleetState,
    HubSnapshot,
    LedgerView,
    ObligationCall,
    PriceSignal,
    Schedule,
)

_T0 = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
_SERVICES = ("DIST_DEFERRAL", "ERCOT_AS", "ERCOT_ENERGY", "PARTNER_CAPACITY")
_TIERS = ("T1", "T2", "T3", "T4")


def _build_fleet(n_hubs: int, n_banks: int) -> tuple[FleetState, LedgerView, Schedule]:
    hubs = []
    banks = []
    calls = []
    prices = []
    hubs_per_bank = max(n_hubs // n_banks, 1)
    for b in range(n_banks):
        bank_id = f"bank-{b}"
        bank_hub_ids = []
        for i in range(hubs_per_bank):
            hub_id = f"hub-{b}-{i}"
            bank_hub_ids.append(hub_id)
            hubs.append(HubSnapshot(hub_id=hub_id, bank_id=bank_id, free_discharge_kw=5.0, tau=1.0))
        capability_kw = 5.0 * hubs_per_bank
        banks.append(BankSnapshot(bank_id=bank_id, capability_kw=capability_kw, kva_rating=capability_kw))
        prices.append(PriceSignal(bank_id=bank_id, price_usd_per_mwh=25.0 + b))
        # A handful of concurrent obligations per bank, spread across tiers/services.
        for j, (tier, service) in enumerate(zip(_TIERS, _SERVICES, strict=True)):
            eligible = tuple(bank_hub_ids[j::4]) or tuple(bank_hub_ids[:1])
            calls.append(
                ObligationCall(
                    obligation_id=f"ob-{b}-{j}",
                    bank_id=bank_id,
                    service_type=service,
                    tier=tier,
                    committed_kw=capability_kw / 8.0,
                    eligible_hub_ids=eligible,
                )
            )
    return (
        FleetState(hubs=tuple(hubs), banks=tuple(banks)),
        LedgerView(calls=tuple(calls)),
        Schedule(prices=tuple(prices)),
    )


def _time_cycle(n_hubs: int, n_banks: int, *, repeats: int = 5) -> float:
    """Best-of-`repeats` wall-clock timing (ms) for one cycle, after a warm-up run. Best-of-N
    smooths over scheduler/GC noise on a shared CI box without hiding a genuine regression -- a
    regression shows up in every repeat, noise only in some.
    """
    fleet_state, ledger_view, schedule = _build_fleet(n_hubs, n_banks)
    cycle(_T0, fleet_state, ledger_view, schedule, {}, ())  # warm up numpy/import paths
    best_ms = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        cycle(_T0, fleet_state, ledger_view, schedule, {}, ())
        best_ms = min(best_ms, (time.perf_counter() - start) * 1000.0)
    return best_ms


def test_ts_05_70_cycle_under_200ms_for_2000_hubs_40_banks(record_property) -> None:
    elapsed_ms = _time_cycle(2000, 40)
    record_property("cycle_ms_2000_hubs_40_banks", elapsed_ms)
    assert elapsed_ms < 200.0


def test_ts_05_71_cycle_timing_measured_for_10000_hubs(record_property) -> None:
    elapsed_ms = _time_cycle(10_000, 40)
    record_property("cycle_ms_10000_hubs_40_banks", elapsed_ms)
    # Not a hard gate at this scale (BUILD.md S5 only requires *measuring* 10k), but flag a gross
    # regression so it doesn't silently balloon.
    assert elapsed_ms < 2000.0
