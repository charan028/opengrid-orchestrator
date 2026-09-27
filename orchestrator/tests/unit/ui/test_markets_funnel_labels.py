"""#43 B6: nothing is sent to ERCOT (`integrations.build_market_submission` is never called), so the Markets
funnel must not say "Bid funnel", "Submitted" or "won". It renders as a simulated offer funnel."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import opengrid.ui as ui
import opengrid.ui.routes.markets as markets

_FIXTURES = Path(__file__).parent / "fixtures"


def _markets_html(monkeypatch: pytest.MonkeyPatch, funnel: Any) -> str:
    async def fake_get_json(path: str, *, params: dict[str, Any] | None = None) -> Any:
        if path == "/og/api/markets/bid-funnel":
            return funnel
        if path == "/og/api/markets/series":
            return []
        return {"feeds": []} if path.endswith("/health") else {"ts": [], "p10": [], "p50": [], "p90": []}

    monkeypatch.setattr(markets, "get_json", fake_get_json)
    app = FastAPI()
    app.include_router(ui.build_router(), prefix="/og")
    response = TestClient(app).get("/og/markets")
    assert response.status_code == 200
    html = response.text
    return html[html.index('id="funnel-heading"') : html.index('id="freshness-heading"')]


def test_funnel_says_selected_not_submitted(monkeypatch: pytest.MonkeyPatch) -> None:
    funnel = json.loads((_FIXTURES / "markets_bid_funnel.json").read_text(encoding="utf-8"))
    panel = _markets_html(monkeypatch, funnel)
    assert "Offer funnel (simulated)" in panel
    assert "Selected by optimizer (not sent to ERCOT)" in panel
    assert "Selected (not sent)" in panel and "Committed" in panel
    for misleading in ("Bid funnel", "Submitted", "We submitted", "We won", "Win rate", "our bids", "bid on"):
        assert misleading not in panel


def test_empty_funnel_does_not_promise_submissions(monkeypatch: pytest.MonkeyPatch) -> None:
    panel = _markets_html(monkeypatch, {})
    assert "No offer funnel yet" in panel and "Nothing is sent to ERCOT" in panel
    assert "bid we submit" not in panel and "Bid funnel" not in panel
