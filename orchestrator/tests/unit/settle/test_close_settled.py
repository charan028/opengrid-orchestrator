"""`FULFILLED/SHORTFALL -> SETTLED` (02a S2.1): lead review 2026-09-26 -- og-settle metered and billed
but never closed an obligation. It now settles every obligation whose window is fully metered and
billed, through `opengrid.contracts` (traced), and skips (retries later) one it cannot move."""

from __future__ import annotations

from uuid import uuid4

import pytest

import opengrid.contracts as contracts
from opengrid.contracts import ConcurrentUpdateError
from opengrid.settle import close_settled_obligations

from .conftest import FakeSettleBackend

pytestmark = pytest.mark.asyncio


async def test_settleable_obligations_move_to_settled_with_r_settled(
    fake_backend: FakeSettleBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    ok, raced = uuid4(), uuid4()
    fake_backend.settleable = [ok, raced]
    calls: list[tuple[object, str, str | None]] = []

    async def _transition(obligation_id, to_state, *, reason_code, payload=None, at_risk=None):
        if obligation_id == raced:
            raise ConcurrentUpdateError(obligation_id, 3)
        calls.append((obligation_id, to_state, reason_code))

    monkeypatch.setattr(contracts, "transition_obligation", _transition)

    assert await close_settled_obligations() == 1
    assert calls == [(ok, "SETTLED", "R-SETTLED")]
