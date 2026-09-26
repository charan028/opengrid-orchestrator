"""D-29 wiring: `run_intake_gate` reserves a TOLLING contract's daily window via `contracts.tolling`, and the
tolling config is read once at intake configuration from `[contracts.tolling]`."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from opengrid.contracts import intake
from opengrid.contracts.tolling import TollingConfig
from opengrid.trace import TraceStore

from ..conftest import make_contract, make_product_rule
from ..fakes import FakeContractsRepo
from .fakes import FakeMarketDataPort

CT = ZoneInfo("America/Chicago")


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    yield
    intake.reset_for_testing()


async def _seed_toll(repo: FakeContractsRepo, *, minutes: int = 90) -> None:
    toll = make_contract(service_type="REGULATED_CAPACITY", variant="TOLLING", tier="T1")
    toll = toll.model_copy(update={"market": "REGULATED", "utility_id": "AUSTIN_ENERGY"})
    await repo.upsert_contract(toll)
    await repo.upsert_product_rule(
        make_product_rule(
            toll.contract_id,
            product_code="REG_CAPACITY",
            min_qty_kw=Decimal("24000"),
            block=True,
            variable_kind="BINARY",
            duration_minutes=minutes,
        )
    )


async def test_gate_reserves_the_toll_window_once(
    repo: FakeContractsRepo, trace: TraceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime

    monkeypatch.delenv("OG_CONFIG", raising=False)
    intake.configure(repo, trace, FakeMarketDataPort(energy_prices={}, as_mcpc={}))
    await _seed_toll(repo)
    now = datetime(2026, 9, 28, 12, 0, tzinfo=CT)
    created = await intake.run_intake_gate("SCHEDULED_15MIN", now=now)
    assert len(created) == 2  # today 16:30-18:00 and tomorrow
    assert all(o.requested_kw == Decimal("24000") for o in created)
    assert await intake.run_intake_gate("SCHEDULED_15MIN", now=now) == []  # idempotent across gates


async def test_explicit_tolling_config_is_used(repo: FakeContractsRepo, trace: TraceStore) -> None:
    from datetime import datetime

    cfg = TollingConfig(window_start_local=time(17, 0), window_end_local=time(18, 0), days_ahead=0)
    intake.configure(repo, trace, FakeMarketDataPort(energy_prices={}, as_mcpc={}), tolling=cfg)
    await _seed_toll(repo, minutes=60)
    created = await intake.run_intake_gate("SCHEDULED_15MIN", now=datetime(2026, 9, 28, 12, 0, tzinfo=CT))
    assert [o.window_start.astimezone(CT).hour for o in created] == [17]


def test_tolling_block_is_read_from_og_config(
    tmp_path: Path, repo: FakeContractsRepo, trace: TraceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    toml = tmp_path / "orchestrator.toml"
    toml.write_text('[contracts.tolling]\nwindow_start = "17:15"\nwindow_end = "18:45"\n', encoding="utf-8")
    monkeypatch.setenv("OG_CONFIG", str(toml))
    intake.configure(repo, trace, FakeMarketDataPort(energy_prices={}, as_mcpc={}))
    state = intake._require_state()
    assert state.tolling.window_start_local == time(17, 15)
    assert state.tolling.window_minutes == 90


def test_repo_config_has_the_tolling_block() -> None:
    import tomllib

    config = Path(__file__).resolve().parents[4] / "config" / "orchestrator.toml"
    block = tomllib.loads(config.read_text(encoding="utf-8"))["contracts"]["tolling"]
    assert block == {"window_start": "16:30", "window_end": "18:00", "days_ahead": 1, "days": "ALL"}


def test_malformed_tolling_block_stops_configuration(
    tmp_path: Path, repo: FakeContractsRepo, trace: TraceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    toml = tmp_path / "orchestrator.toml"
    toml.write_text('[contracts.tolling]\nwindow_start = "late"\n', encoding="utf-8")
    monkeypatch.setenv("OG_CONFIG", str(toml))
    with pytest.raises(ValueError, match="window_start"):
        intake.configure(repo, trace, FakeMarketDataPort(energy_prices={}, as_mcpc={}))
