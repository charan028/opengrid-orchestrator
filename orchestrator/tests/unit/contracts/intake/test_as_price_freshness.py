"""AS intake refuses a stale np4-188-cd clearing price (review finding): `[contracts.intake].as_price_max_age_s`,
traced `INTAKE_SKIPPED` / `R-AS-PRICE-STALE`."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from opengrid.contracts import intake
from opengrid.contracts.intake import ancillary
from opengrid.trace import TraceStore

from ..conftest import make_contract, make_product_rule
from ..fakes import FakeContractsRepo, FakeTraceBackend
from .fakes import FakeMarketDataPort

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("OG_CONFIG", raising=False)
    yield
    intake.reset_for_testing()


async def _seed_as(repo: FakeContractsRepo) -> None:
    contract = make_contract(service_type="ERCOT_AS")
    await repo.upsert_contract(contract)
    await repo.upsert_product_rule(
        make_product_rule(
            contract.contract_id,
            product_code="NONSPIN",
            min_qty_kw=Decimal("100"),
            increment_kw=Decimal("100"),
            variable_kind="SEMI_CONTINUOUS",
        )
    )


def _market(price_ts: datetime) -> FakeMarketDataPort:
    return FakeMarketDataPort(as_mcpc={"NONSPIN": 20.0}, as_mcpc_ts={"NONSPIN": price_ts})


async def test_fresh_price_offers_as(repo: FakeContractsRepo, trace: TraceStore) -> None:
    intake.configure(repo, trace, _market(NOW - timedelta(hours=20)))
    await _seed_as(repo)
    assert await intake.run_intake_gate("SCHEDULED_15MIN", now=NOW)


async def test_day_ahead_price_in_the_future_is_fresh(repo: FakeContractsRepo, trace: TraceStore) -> None:
    intake.configure(repo, trace, _market(NOW + timedelta(hours=10)))
    await _seed_as(repo)
    assert await intake.run_intake_gate("SCHEDULED_15MIN", now=NOW)


async def test_stale_price_skips_as_and_traces_the_reason(
    repo: FakeContractsRepo, trace: TraceStore, trace_backend: FakeTraceBackend
) -> None:
    intake.configure(repo, trace, _market(NOW - timedelta(days=3)))
    await _seed_as(repo)
    assert await intake.run_intake_gate("SCHEDULED_15MIN", now=NOW) == []
    assert repo.opportunities == {}
    rows = [r for r in trace_backend.rows if r.get("event_class") == "INTAKE_SKIPPED"]
    assert len(rows) == 1
    assert rows[0]["decision_type"] == "ALERT"
    assert list(rows[0]["reason_codes"]) == [intake.R_AS_PRICE_STALE]


async def test_configured_bound_applies(repo: FakeContractsRepo, trace: TraceStore) -> None:
    intake.configure(repo, trace, _market(NOW - timedelta(hours=2)), as_price_max_age_s=3600)
    await _seed_as(repo)
    assert await intake.run_intake_gate("SCHEDULED_15MIN", now=NOW) == []


def test_bound_is_read_from_og_config(
    tmp_path: Path, repo: FakeContractsRepo, trace: TraceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    toml = tmp_path / "orchestrator.toml"
    toml.write_text("[contracts.intake]\nas_price_max_age_s = 7200\n", encoding="utf-8")
    monkeypatch.setenv("OG_CONFIG", str(toml))
    intake.configure(repo, trace, _market(NOW))
    assert intake._require_state().as_price_max_age_s == 7200.0
    toml.write_text("[contracts.intake]\nas_price_max_age_s = 0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="as_price_max_age_s"):
        intake.configure(repo, trace, _market(NOW))


def test_default_bound_and_repo_config() -> None:
    import tomllib

    assert intake.DEFAULT_AS_PRICE_MAX_AGE_S == 93_600.0
    config = Path(__file__).resolve().parents[4] / "config" / "orchestrator.toml"
    block = tomllib.loads(config.read_text(encoding="utf-8"))["contracts"]["intake"]
    assert block == {"as_price_max_age_s": 93600}


async def test_intake_prices_each_hour_at_its_own_mcpc(repo: FakeContractsRepo, trace: TraceStore) -> None:
    """Issue #43 A1: the posted hourly np4-188-cd MCPCs price their own hours (not the latest HE24
    for all 24); an unposted hour takes the latest, which is still subject to the staleness bound."""
    day_start = ancillary.next_operating_day_start(NOW)
    hourly = {day_start + timedelta(hours=h): 5.0 + h for h in range(ancillary.OPERATING_DAY_HOURS - 1)}
    market = _market(NOW - timedelta(hours=1))  # latest MCPC 20.0
    market.as_mcpc_hourly = {"NONSPIN": hourly}
    intake.configure(repo, trace, market)
    await _seed_as(repo)
    created = await intake.run_intake_gate("SCHEDULED_15MIN", now=NOW)
    value_by_start = {o.window_start: o.value_per_mwh for o in created}
    assert value_by_start[day_start] == Decimal("5.0")
    assert value_by_start[day_start + timedelta(hours=22)] == Decimal("27.0")
    assert value_by_start[day_start + timedelta(hours=23)] == Decimal("20.0")  # not posted: latest


async def test_hourly_prices_do_not_bypass_the_staleness_bound(
    repo: FakeContractsRepo, trace: TraceStore
) -> None:
    day_start = ancillary.next_operating_day_start(NOW)
    market = _market(NOW - timedelta(days=3))
    market.as_mcpc_hourly = {"NONSPIN": {day_start: 12.0}}
    intake.configure(repo, trace, market)
    await _seed_as(repo)
    assert await intake.run_intake_gate("SCHEDULED_15MIN", now=NOW) == []
