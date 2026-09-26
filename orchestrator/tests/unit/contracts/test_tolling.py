"""Utility tolling daily reservation (D-29, issue #36): `opengrid.contracts.tolling`."""

from __future__ import annotations

from datetime import UTC, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from opengrid.contracts.tolling import (
    TollingConfig,
    plan_windows,
    run_tolling,
    run_tolling_contract,
    tolling_config_from,
)
from opengrid.core.models.engine import Contract
from opengrid.market.capacity import capacity_value_usd_per_mwh
from opengrid.trace import TraceStore

from .conftest import make_contract, make_product_rule
from .fakes import FakeContractsRepo

CT = ZoneInfo("America/Chicago")
CFG = TollingConfig()  # 16:30-18:00 CT, today + 1 day


class _Cfg:
    def __init__(self, data: dict[str, object]) -> None:
        self._data = data

    def get(self, key: str, default: object) -> object:
        return self._data.get(key, default)


def _toll(
    repo: FakeContractsRepo, *, kw: str = "24000", minutes: int = 90, **kw_contract: object
) -> Contract:
    contract = make_contract(service_type="REGULATED_CAPACITY", variant="TOLLING", tier="T1")
    contract = contract.model_copy(
        update={"market": "REGULATED", "utility_id": "AUSTIN_ENERGY", **kw_contract}
    )
    repo.contracts[contract.contract_id] = contract
    repo.product_rules[contract.contract_id] = [
        make_product_rule(
            contract.contract_id,
            product_code="REG_CAPACITY",
            min_qty_kw=Decimal(kw),
            block=True,
            variable_kind="BINARY",
            duration_minutes=minutes,
        )
    ]
    return contract


def test_plan_windows_today_and_tomorrow_in_local_time() -> None:
    now = datetime(2026, 9, 28, 12, 0, tzinfo=CT)
    windows = plan_windows(now, CFG)
    assert [(s.astimezone(CT), e.astimezone(CT)) for s, e in windows] == [
        (datetime(2026, 9, 28, 16, 30, tzinfo=CT), datetime(2026, 9, 28, 18, 0, tzinfo=CT)),
        (datetime(2026, 9, 29, 16, 30, tzinfo=CT), datetime(2026, 9, 29, 18, 0, tzinfo=CT)),
    ]


def test_plan_windows_skips_a_started_window_and_weekends() -> None:
    started = datetime(2026, 9, 28, 16, 45, tzinfo=CT)
    assert [s.astimezone(CT).day for s, _ in plan_windows(started, CFG)] == [29]
    friday_evening = datetime(2026, 10, 2, 19, 0, tzinfo=CT)
    weekdays = TollingConfig(days="WEEKDAYS", days_ahead=3)
    assert [s.astimezone(CT).day for s, _ in plan_windows(friday_evening, weekdays)] == [5]


def test_plan_windows_keeps_local_time_across_dst() -> None:
    windows = plan_windows(datetime(2026, 10, 31, 12, 0, tzinfo=CT), CFG)  # DST ends 2026-11-01
    assert [s.astimezone(CT).hour for s, _ in windows] == [16, 16]
    assert [s.astimezone(UTC).hour for s, _ in windows] == [21, 22]


def test_config_parsing_and_validation() -> None:
    cfg = tolling_config_from(
        _Cfg({"contracts.tolling.window_start": "17:00", "contracts.tolling.window_end": "18:30"})
    )
    assert cfg.window_start_local == time(17, 0)
    assert cfg.window_minutes == 90
    assert tolling_config_from(object()) == TollingConfig()
    with pytest.raises(ValueError):
        tolling_config_from(_Cfg({"contracts.tolling.window_start": "5pm"}))
    with pytest.raises(ValueError):
        tolling_config_from(_Cfg({"contracts.tolling.days": "SOMETIMES"}))
    with pytest.raises(ValueError):
        TollingConfig(window_start_local=time(18, 0), window_end_local=time(16, 30))


async def test_reserves_each_window_once_through_admission(
    repo: FakeContractsRepo, trace: TraceStore
) -> None:
    contract = _toll(repo)
    now = datetime(2026, 9, 28, 12, 0, tzinfo=CT)
    created = await run_tolling(repo, trace, config=CFG, now=now)
    assert len(created) == 2
    assert all(o.requested_kw == Decimal("24000") and o.state == "OFFERED" for o in created)
    assert created[0].value_per_mwh == capacity_value_usd_per_mwh(Decimal("102"), "USD_PER_KW_YEAR")
    obligations = list(repo.obligations.values())
    assert {o.service_type for o in obligations} == {"REGULATED_CAPACITY"}
    assert all(o.contract_id == contract.contract_id for o in obligations)
    # Idempotent: a second run (e.g. the next gate) creates nothing.
    assert await run_tolling(repo, trace, config=CFG, now=now) == []
    assert len(repo.opportunities) == 2


async def test_non_tolling_and_inactive_contracts_are_ignored(
    repo: FakeContractsRepo, trace: TraceStore
) -> None:
    _toll(repo, variant="POWER_PARTNER")
    _toll(repo, status="SUSPENDED")
    assert await run_tolling(repo, trace, config=CFG, now=datetime(2026, 9, 28, 12, 0, tzinfo=CT)) == []


async def test_mis_sized_rule_fails_closed(repo: FakeContractsRepo, trace: TraceStore) -> None:
    now = datetime(2026, 9, 28, 12, 0, tzinfo=CT)
    wrong_window = _toll(repo, minutes=60)
    assert await run_tolling_contract(repo, trace, wrong_window, now=now, config=CFG) == []
    zero = _toll(repo, kw="0")
    assert await run_tolling_contract(repo, trace, zero, now=now, config=CFG) == []
    assert repo.opportunities == {}


async def test_unknown_utility_is_admitted_at_zero_value(repo: FakeContractsRepo, trace: TraceStore) -> None:
    contract = _toll(repo, market="FREE", utility_id=None)  # e.g. a repo row read without migration 0025
    created = await run_tolling_contract(
        repo, trace, contract, now=datetime(2026, 9, 28, 12, 0, tzinfo=CT), config=CFG
    )
    assert created and all(o.value_per_mwh == 0 for o in created)


def test_capacity_value_matches_the_pro_rated_payment() -> None:
    per_mwh = capacity_value_usd_per_mwh(Decimal("102"), "USD_PER_KW_YEAR")
    assert per_mwh == Decimal("102") / Decimal("8760") * Decimal("1000")
    assert capacity_value_usd_per_mwh(Decimal("8.5"), "USD_PER_KW_MONTH") == per_mwh
    assert capacity_value_usd_per_mwh(None, "USD_PER_KW_YEAR") == 0
