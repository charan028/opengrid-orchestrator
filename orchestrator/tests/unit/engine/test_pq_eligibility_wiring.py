"""WP-D wiring (owner decision 2026-09-26): PQ-sensitive profiles (DATA_CENTER) draw only on PQ-eligible
hubs, are never committed beyond that capacity, and fail closed before characterization data exists;
arbitrage/ERCOT services are unaffected."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from opengrid import ledger
from opengrid.engine import pq_eligibility as pe
from opengrid.selector import gate
from opengrid.selector.types import CandidateOpportunity


def _row(hub_id, bank_id, *, quality=0.9, health="online", state="OK", rated=11.0):
    # _ROWS_SQL shape
    fields = (rated, "ABC", 12.0, 0.9, 0.9, 0.0, 0.2, 3.0, 0.0, quality, "CATEGORY_III", state, health)
    return (hub_id, bank_id, *fields)


@pytest.fixture(autouse=True)
def _reset():
    pe.configure(pe.load_profile_configs())
    yield
    pe.configure({})
    gate.configure_pq_capacity(None)


def test_the_data_center_profile_is_pq_sensitive_and_arbitrage_is_not() -> None:
    assert pe.is_pq_sensitive("DATA_CENTER")
    assert not pe.is_pq_sensitive("ERCOT_ENERGY")
    assert not pe.is_pq_sensitive("ERCOT_AS")


def test_compute_keeps_only_online_hubs_above_the_quality_floor() -> None:
    rows = [
        _row("h1", "b1"),
        _row("h2", "b1", quality=0.5),  # below min_quality_score 0.8
        _row("h3", "b1", health="stale"),
        _row("h4", "b2", rated=20.0),
    ]
    state = pe.compute(rows, pe.load_profile_configs())["DATA_CENTER"]
    assert state.hubs_by_bank == {"b1": {"h1"}, "b2": {"h4"}}
    assert state.kw_by_bank == {"b1": 11.0, "b2": 20.0}


def test_filtering_fails_closed_before_the_first_refresh_and_passes_other_services() -> None:
    assert pe.filter_hub_ids("DATA_CENTER", "b1", ("h1", "h2")) == ()
    assert pe.eligible_kw("DATA_CENTER", "b1") == 0.0
    assert pe.filter_hub_ids("ERCOT_AS", "b1", ("h1", "h2")) == ("h1", "h2")
    assert pe.eligible_kw("ERCOT_AS", "b1") is None


def _candidate(service_type: str) -> CandidateOpportunity:
    return CandidateOpportunity(
        opportunity_id="op",
        obligation_id="ob",
        contract_id="c",
        eligible_bank_ids=("b1",),
        window_intervals=(0,),
        requested_kw=50.0,
        value_per_mwh=1.0,
        variable_kind="CONTINUOUS",
        min_qty_kw=0.0,
        increment_kw=0.0,
        service_type=service_type,
    )


def test_a_data_center_selection_beyond_eligible_capacity_is_not_committed() -> None:
    gate.configure_pq_capacity(lambda service_type, bank_id: 40.0 if service_type == "DATA_CENTER" else None)
    start = datetime(2026, 9, 26, 12, tzinfo=UTC)
    key = ledger.encode_interval_key("b1", start, start + timedelta(minutes=15))

    assert gate.exceeds_pq_eligible_capacity(_candidate("DATA_CENTER"), {key: Decimal("50")})
    assert not gate.exceeds_pq_eligible_capacity(_candidate("DATA_CENTER"), {key: Decimal("40")})
    assert not gate.exceeds_pq_eligible_capacity(_candidate("ERCOT_AS"), {key: Decimal("500")})
