"""`gate.load_candidates`/`gate._configured_bank_ids`: real sources, no more placeholders.

`load_candidates` shapes `selector.db`'s raw `og.opportunity` rows into `CandidateOpportunity`s
(BUILD.md fix: gate.load_candidates must use a real source, not always return `()`); `_configured_bank_ids`
derives the `bank-NN` id list from `[fleet].banks` instead of always returning `()`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from opengrid.selector import db, gate

HORIZON_START = datetime(2026, 9, 26, 0, 0, tzinfo=UTC)
HORIZON_END = HORIZON_START + timedelta(hours=24)


def _row(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "opportunity_id": uuid4(),
        "contract_id": uuid4(),
        "window_start": HORIZON_START,
        "window_end": HORIZON_START + timedelta(minutes=30),
        "requested_kw": Decimal("10.0"),
        "value_per_mwh": Decimal("100.0"),
        "service_type": "ERCOT_AS",
        "tier": "T2",
        "degradation_cost": Decimal("0.03"),
        "variable_kind": "BINARY",
        "min_qty_kw": Decimal("0"),
        "increment_kw": Decimal("0"),
    }
    base.update(overrides)
    return base


async def test_load_candidates_shapes_offered_opportunity_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _row()

    async def _fake_rows(horizon_start, horizon_end, contract_scope):
        assert contract_scope is None
        return [row]

    monkeypatch.setattr(db, "load_offered_opportunities_rows", _fake_rows)

    candidates = await gate.load_candidates(HORIZON_START, HORIZON_END, ("bank-01",), None)

    assert len(candidates) == 1
    c = candidates[0]
    assert c.opportunity_id == str(row["opportunity_id"])
    assert c.contract_id == str(row["contract_id"])
    assert c.eligible_bank_ids == ("bank-01",)
    assert c.window_intervals == (0, 1)  # 30 minutes / 15-min intervals
    assert c.requested_kw == 10.0
    assert c.value_per_mwh == 100.0
    assert c.variable_kind == "BINARY"
    assert c.category == "AS"  # ERCOT_AS -> AS bucket
    assert c.tier == "T2"


async def test_load_candidates_defaults_missing_product_rule_to_continuous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _row(variable_kind=None, min_qty_kw=None, increment_kw=None, service_type="HOME", tier=None)

    async def _fake_rows(horizon_start, horizon_end, contract_scope):
        return [row]

    monkeypatch.setattr(db, "load_offered_opportunities_rows", _fake_rows)

    candidates = await gate.load_candidates(HORIZON_START, HORIZON_END, ("bank-01",), None)

    assert candidates[0].variable_kind == "CONTINUOUS"
    assert candidates[0].min_qty_kw == 0.0
    assert candidates[0].increment_kw == 0.0
    assert candidates[0].category == "FIRM"  # HOME -> FIRM
    assert candidates[0].tier == "T4"  # default


async def test_load_candidates_drops_a_row_whose_window_does_not_overlap_the_horizon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _row(
        window_start=HORIZON_START - timedelta(hours=2),
        window_end=HORIZON_START - timedelta(hours=1),
    )

    async def _fake_rows(horizon_start, horizon_end, contract_scope):
        return [row]

    monkeypatch.setattr(db, "load_offered_opportunities_rows", _fake_rows)

    candidates = await gate.load_candidates(HORIZON_START, HORIZON_END, ("bank-01",), None)

    assert candidates == ()


async def test_configured_bank_ids_reads_real_bank_ids_from_og_bank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_configured_bank_ids` must read the real topology from `og.bank` (seeded by
    `opengrid.fleet.seed` with `bank-000`..`bank-039`-style ids), never fabricate a `bank-NN` list from
    a configured count (qa/merge-notes.md section 11 -- the two id spaces are disjoint)."""

    async def _fake_bank_ids_rows() -> list[str]:
        return ["bank-000", "bank-001", "bank-002"]

    monkeypatch.setattr(db, "load_bank_ids_rows", _fake_bank_ids_rows)

    bank_ids = await gate._configured_bank_ids()

    assert bank_ids == ("bank-000", "bank-001", "bank-002")


async def test_configured_bank_ids_never_fabricates_ids_not_returned_by_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: whatever `og.bank` returns is passed through verbatim -- no `bank-NN`-style id is
    synthesised from a count, even when the real ids use an unexpected count or format."""

    async def _fake_bank_ids_rows() -> list[str]:
        return ["bank-007"]

    monkeypatch.setattr(db, "load_bank_ids_rows", _fake_bank_ids_rows)

    bank_ids = await gate._configured_bank_ids()

    assert bank_ids == ("bank-007",)
    assert "bank-01" not in bank_ids
    assert "bank-00" not in bank_ids
