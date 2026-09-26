"""Shared builders for selector tests: small hand-computable `ModelInputs` factories."""

from __future__ import annotations

from opengrid.selector.types import (
    BankSnapshot,
    CandidateOpportunity,
    CommittedObligation,
    ModelInputs,
    ScenarioPrice,
)


def make_bank(bank_id: str, capacity_kw: float, intervals: range) -> BankSnapshot:
    return BankSnapshot(bank_id=bank_id, max_discharge_kw=dict.fromkeys(intervals, capacity_kw))


def zero_price_scenario(intervals: range, scenario: str = "P50", probability: float = 1.0) -> ScenarioPrice:
    return ScenarioPrice(
        scenario=scenario, probability=probability, price_usd_per_mwh=dict.fromkeys(intervals, 0.0)
    )


def three_point_scenarios(intervals: range, prices: dict[str, float]) -> tuple[ScenarioPrice, ...]:
    weights = {"P10": 0.25, "P50": 0.5, "P90": 0.25}
    return tuple(
        ScenarioPrice(
            scenario=name, probability=weight, price_usd_per_mwh=dict.fromkeys(intervals, prices[name])
        )
        for name, weight in weights.items()
    )


def binary_candidate(
    opportunity_id: str,
    requested_kw: float,
    value_per_mwh: float,
    window: tuple[int, ...],
    eligible_banks: tuple[str, ...],
    *,
    category: str = "MARKET",
    degradation_cost_per_kwh: float = 0.0,
) -> CandidateOpportunity:
    return CandidateOpportunity(
        opportunity_id=opportunity_id,
        obligation_id=opportunity_id,
        contract_id=f"contract-{opportunity_id}",
        eligible_bank_ids=eligible_banks,
        window_intervals=window,
        requested_kw=requested_kw,
        value_per_mwh=value_per_mwh,
        variable_kind="BINARY",
        min_qty_kw=0.0,
        increment_kw=0.0,
        category=category,  # type: ignore[arg-type]
        degradation_cost_per_kwh=degradation_cost_per_kwh,
    )


def continuous_candidate(
    opportunity_id: str,
    requested_kw: float,
    value_per_mwh: float,
    window: tuple[int, ...],
    eligible_banks: tuple[str, ...],
    *,
    category: str = "MARKET",
    degradation_cost_per_kwh: float = 0.0,
) -> CandidateOpportunity:
    return CandidateOpportunity(
        opportunity_id=opportunity_id,
        obligation_id=opportunity_id,
        contract_id=f"contract-{opportunity_id}",
        eligible_bank_ids=eligible_banks,
        window_intervals=window,
        requested_kw=requested_kw,
        value_per_mwh=value_per_mwh,
        variable_kind="CONTINUOUS",
        min_qty_kw=0.0,
        increment_kw=0.0,
        category=category,  # type: ignore[arg-type]
        degradation_cost_per_kwh=degradation_cost_per_kwh,
    )


def semi_continuous_candidate(
    opportunity_id: str,
    requested_kw: float,
    min_qty_kw: float,
    increment_kw: float,
    value_per_mwh: float,
    window: tuple[int, ...],
    eligible_banks: tuple[str, ...],
    *,
    category: str = "MARKET",
    degradation_cost_per_kwh: float = 0.0,
) -> CandidateOpportunity:
    return CandidateOpportunity(
        opportunity_id=opportunity_id,
        obligation_id=opportunity_id,
        contract_id=f"contract-{opportunity_id}",
        eligible_bank_ids=eligible_banks,
        window_intervals=window,
        requested_kw=requested_kw,
        value_per_mwh=value_per_mwh,
        variable_kind="SEMI_CONTINUOUS",
        min_qty_kw=min_qty_kw,
        increment_kw=increment_kw,
        category=category,  # type: ignore[arg-type]
        degradation_cost_per_kwh=degradation_cost_per_kwh,
    )


def committed(
    obligation_id: str, committed_kw_by_interval: dict[int, float], eligible_banks: tuple[str, ...]
) -> CommittedObligation:
    return CommittedObligation(
        obligation_id=obligation_id,
        eligible_bank_ids=eligible_banks,
        committed_kw_by_interval=committed_kw_by_interval,
    )


def simple_inputs(
    banks: tuple[BankSnapshot, ...],
    scenarios: tuple[ScenarioPrice, ...],
    committed_obligations: tuple[CommittedObligation, ...],
    candidates: tuple[CandidateOpportunity, ...],
    n_intervals: int,
    interval_minutes: float = 15.0,
) -> ModelInputs:
    return ModelInputs(
        intervals=tuple(range(n_intervals)),
        interval_minutes=interval_minutes,
        banks=banks,
        scenarios=scenarios,
        committed=committed_obligations,
        candidates=candidates,
    )
