"""Integration-level tests for `opengrid.contracts.intake.run_intake_gate` against fakes (BUILD.md
"use fakes for siblings"): the task brief's required multi-customer, idempotency, and K13 no-overlap
coverage.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from opengrid.contracts import intake
from opengrid.contracts.intake.energy import ENERGY_HORIZON_INTERVALS
from opengrid.core.models.engine import Obligation
from opengrid.core.timeutil import floor_to_interval
from opengrid.trace import TraceStore

from ..conftest import make_contract, make_product_rule
from ..fakes import FakeContractsRepo
from .fakes import FakeForecastScenarios, FakeMarketDataPort

ENERGY_SERIES = intake.DEFAULT_ENERGY_SERIES_KEY
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
_HORIZON_START = floor_to_interval(NOW) + timedelta(minutes=15)


def _forecast_prices() -> dict[datetime, float]:
    # Every discharge interval at $50/MWh; combined with a $10/MWh charge price and the default 0.90
    # eta_rt/$0.03-per-kWh degradation this clears a positive spread (see test_energy.py's hand
    # computation), so every horizon interval is a candidate.
    return {_HORIZON_START + timedelta(minutes=15 * i): 50.0 for i in range(ENERGY_HORIZON_INTERVALS)}


@pytest.fixture
def market() -> FakeMarketDataPort:
    return FakeMarketDataPort(energy_prices={ENERGY_SERIES: 10.0}, as_mcpc={"NONSPIN": 20.0})


@pytest.fixture(autouse=True)
def _configure_intake(
    repo: FakeContractsRepo, trace: TraceStore, market: FakeMarketDataPort
) -> Iterator[None]:
    forecast = FakeForecastScenarios(series_key=ENERGY_SERIES, price_by_interval=_forecast_prices())
    intake.configure(repo, trace, market, forecast_scenarios=forecast)
    yield
    intake.reset_for_testing()


async def _seed_five_customers(repo: FakeContractsRepo) -> dict[str, object]:
    home = make_contract(service_type="HOME")
    energy_contract = make_contract(service_type="ERCOT_ENERGY")
    as_contract = make_contract(service_type="ERCOT_AS")
    deferral_contract = make_contract(service_type="DIST_DEFERRAL")
    partner_contract = make_contract(service_type="PARTNER_CAPACITY")
    for c in (home, energy_contract, as_contract, deferral_contract, partner_contract):
        await repo.upsert_contract(c)
    await repo.upsert_product_rule(
        make_product_rule(energy_contract.contract_id, product_code="ENERGY", variable_kind="CONTINUOUS")
    )
    await repo.upsert_product_rule(
        make_product_rule(
            as_contract.contract_id,
            product_code="NONSPIN",
            min_qty_kw=Decimal("100"),
            increment_kw=Decimal("100"),
            variable_kind="SEMI_CONTINUOUS",
        )
    )
    await repo.upsert_product_rule(
        make_product_rule(
            deferral_contract.contract_id,
            product_code="CAPACITY_HOLD",
            min_qty_kw=Decimal("1"),
            increment_kw=Decimal("1"),
            variable_kind="SEMI_CONTINUOUS",
        )
    )
    return {
        "home": home,
        "energy": energy_contract,
        "as": as_contract,
        "deferral": deferral_contract,
        "partner": partner_contract,
    }


async def test_five_customers_produce_opportunities_in_one_gate(repo: FakeContractsRepo) -> None:
    contracts_by_role = await _seed_five_customers(repo)

    created = await intake.run_intake_gate("SCHEDULED_15MIN", now=NOW)

    created_contract_ids = {o.contract_id for o in created}
    assert contracts_by_role["energy"].contract_id in created_contract_ids
    assert contracts_by_role["as"].contract_id in created_contract_ids
    assert contracts_by_role["deferral"].contract_id in created_contract_ids
    # HOME and PARTNER_CAPACITY never get speculative opportunities from intake.
    assert contracts_by_role["home"].contract_id not in created_contract_ids
    assert contracts_by_role["partner"].contract_id not in created_contract_ids
    assert len(created) > 0


async def test_intake_is_idempotent_across_repeated_gates(repo: FakeContractsRepo) -> None:
    await _seed_five_customers(repo)

    first = await intake.run_intake_gate("SCHEDULED_15MIN", now=NOW)
    assert len(first) > 0
    total_after_first = len(repo.opportunities)

    second = await intake.run_intake_gate("SCHEDULED_15MIN", now=NOW)

    assert second == []
    assert len(repo.opportunities) == total_after_first


def test_default_energy_series_key_is_a_load_zone_not_a_hub_code() -> None:
    """Regression (`qa/merge-notes.md` S15): the default was `"HB_HOUSTON"`, a hub code that
    `feeds.ercot` never returns under `settlementPointType=LZ` (`forecast/README.md`'s canonical
    series-key table) -- so `og.feed_obs`/`forecast.scenarios()` only ever carry `LZ_*` keys and the
    energy intake gate silently produced zero opportunities. Guards against reintroducing a hub code."""
    assert intake.DEFAULT_ENERGY_SERIES_KEY.startswith("LZ_")


def test_energy_series_key_for_zone_uses_the_banks_own_zone() -> None:
    """Bug regression: every contract's energy candidates used to be priced against `LZ_HOUSTON`
    regardless of which bank(s) actually backed the capacity. A North bank must price against
    `LZ_NORTH`, not the Houston default."""
    assert intake.energy_series_key_for_zone("LZ_NORTH") == "LZ_NORTH"


def test_energy_series_key_for_zone_falls_back_to_houston_and_logs(caplog) -> None:
    """A missing/unknown bank zone is a documented, logged fallback (BUILD.md S5a "no silent
    fallbacks"), never a silent one."""
    with caplog.at_level("WARNING"):
        result = intake.energy_series_key_for_zone(None)
    assert result == intake.DEFAULT_ENERGY_SERIES_KEY
    assert any("load zone" in record.message for record in caplog.records)


def test_energy_series_key_for_zone_rejects_unknown_zone_strings() -> None:
    """A zone string that isn't one of the fleet's known `FLEET_ZONES` is treated the same as missing
    -- it falls back rather than being passed through to `feeds.ercot` unchecked."""
    assert intake.energy_series_key_for_zone("LZ_MADE_UP") == intake.DEFAULT_ENERGY_SERIES_KEY


async def test_configure_bank_zone_overrides_energy_series_key(
    repo: FakeContractsRepo, trace: TraceStore
) -> None:
    """`configure(..., bank_zone="LZ_NORTH")` must price ERCOT_ENERGY candidates against `LZ_NORTH`,
    not the `LZ_HOUSTON` default -- this is the fix for the "every home priced at Houston" bug."""
    energy_contract = make_contract(service_type="ERCOT_ENERGY")
    await repo.upsert_contract(energy_contract)
    await repo.upsert_product_rule(
        make_product_rule(energy_contract.contract_id, product_code="ENERGY", variable_kind="CONTINUOUS")
    )
    market = FakeMarketDataPort(energy_prices={"LZ_NORTH": 10.0})
    forecast = FakeForecastScenarios(series_key="LZ_NORTH", price_by_interval=_forecast_prices())
    intake.configure(repo, trace, market, forecast_scenarios=forecast, bank_zone="LZ_NORTH")
    try:
        created = await intake.run_intake_gate("SCHEDULED_15MIN", now=NOW)
    finally:
        intake.reset_for_testing()

    assert len(created) > 0
    assert market.energy_prices.get("LZ_HOUSTON") is None  # never priced against Houston


async def test_intake_never_overlaps_a_committed_obligation(repo: FakeContractsRepo) -> None:
    contracts_by_role = await _seed_five_customers(repo)
    energy_contract = contracts_by_role["energy"]

    # Pre-commit exactly the window intake would otherwise offer first (K13: never re-offer/replace
    # committed capacity).
    committed_start = _HORIZON_START
    committed_end = committed_start + timedelta(minutes=15)
    obligation_id = uuid4()
    repo.obligations[obligation_id] = Obligation(
        obligation_id=obligation_id,
        opportunity_id=uuid4(),
        contract_id=energy_contract.contract_id,
        service_type="ERCOT_ENERGY",
        tier=energy_contract.tier,
        window_start=committed_start,
        window_end=committed_end,
        committed_qty_kw=Decimal("500"),
        state="COMMITTED",
    )

    created = await intake.run_intake_gate("SCHEDULED_15MIN", now=NOW)

    energy_windows = {
        (o.window_start, o.window_end) for o in created if o.contract_id == energy_contract.contract_id
    }
    assert (committed_start, committed_end) not in energy_windows
    # every offered opportunity for this contract must genuinely avoid the committed interval.
    for window_start, window_end in energy_windows:
        assert not (window_start < committed_end and window_end > committed_start)
