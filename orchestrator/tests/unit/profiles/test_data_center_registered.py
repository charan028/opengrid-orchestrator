"""DATA_CENTER is a first-class service type (PR #4 follow-up): model literal, migration CHECK, selector
priority bucket and settle meter source all know it, so a DATA_CENTER contract can be admitted, selected
and settled."""

from __future__ import annotations

from pathlib import Path
from typing import get_args

from opengrid.core.models.engine import ServiceType
from opengrid.selector.gate import _CATEGORY_BY_SERVICE_TYPE
from opengrid.settle.baselines import METER_SOURCE_BY_SERVICE

# 0025 owns the full og.contract service_type CHECK list (lead decision 2026-09-26); 0013 only added DATA_CENTER.
MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "0025_market_model.sql"


def test_data_center_is_registered_everywhere_a_service_type_is_mapped() -> None:
    assert "DATA_CENTER" in get_args(ServiceType)
    assert _CATEGORY_BY_SERVICE_TYPE["DATA_CENTER"] == "FIRM"
    assert set(METER_SOURCE_BY_SERVICE) == set(get_args(ServiceType))


def test_migration_widens_the_contract_check_without_dropping_a_value() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    for service_type in get_args(ServiceType):
        assert f"'{service_type}'" in sql
