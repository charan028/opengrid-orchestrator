"""The copilot relay route (issue #26 items 1 and 4).

The degradation path is the point: when the agent is unreachable the route must still answer 200 with
the "assistant unavailable" text, because no screen may be blocked, slowed or marked degraded by the
agent's absence (UI-DSP-13).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import opengrid.ui as ui
import opengrid.ui.routes.copilot as copilot
from opengrid.ui.api_client import ApiUnavailable


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(ui.build_router(), prefix="/og")
    return TestClient(app)


def test_an_answer_is_rendered_with_its_citation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_post(path: str, payload: dict[str, Any], *, remote_user: str | None = None) -> Any:
        assert path == "/og/api/ai/ask"
        return {
            "text": "1 obligation flagged at risk.",
            "tier": "deterministic",
            "citations": [
                {"source": "/og/api/dispatch/opportunities", "ref": "o1", "label": "DIST_DEFERRAL o1"}
            ],
            "model": None,
        }

    monkeypatch.setattr(copilot, "post_json", fake_post)
    response = _client().post("/og/copilot/ask", data={"question": "what is at risk?", "screen": "/og/"})

    assert response.status_code == 200
    assert "1 obligation flagged at risk." in response.text
    assert "/og/api/dispatch/opportunities" in response.text
    assert "no model used" in response.text  # a deterministic answer never claims the AI badge


def test_an_unreachable_agent_still_answers_200(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_post(path: str, payload: dict[str, Any], *, remote_user: str | None = None) -> Any:
        raise ApiUnavailable("connection refused")

    monkeypatch.setattr(copilot, "post_json", fake_post)
    response = _client().post("/og/copilot/ask", data={"question": "anything", "screen": "/og/"})

    assert response.status_code == 200
    assert "Assistant unavailable" in response.text
    assert "deterministic controls unaffected" in response.text


def test_an_ai_answer_carries_the_badge_and_its_model(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_post(path: str, payload: dict[str, Any], *, remote_user: str | None = None) -> Any:
        return {
            "text": "Because it priced below cost.",
            "tier": "prose",
            "citations": [],
            "model": "claude-haiku-4-5-20251001",
            "confidence_label": "High",
        }

    monkeypatch.setattr(copilot, "post_json", fake_post)
    response = _client().post("/og/copilot/ask", data={"question": "why?", "screen": "/og/"})

    assert "AI-assisted" in response.text
    assert "claude-haiku-4-5-20251001" in response.text
    assert "confidence: High" in response.text


def test_a_screened_console_answer_names_the_routing_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Owner report 2026-09-26: a working model was shown as "no model used" whenever the answer text
    came from console data. The routing model that screened the question is named instead."""

    async def fake_post(path: str, payload: dict[str, Any], *, remote_user: str | None = None) -> Any:
        return {
            "text": "700 hubs rated 78.4 kWh.",
            "tier": "deterministic",
            "citations": [{"source": "/og/api/fleet/summary", "ref": "{}", "label": "fleet summary"}],
            "model": None,
            "screened_by": "claude-haiku-4-5-20251001",
        }

    monkeypatch.setattr(copilot, "post_json", fake_post)
    response = _client().post("/og/copilot/ask", data={"question": "how many units have capacity 78.4 kWh"})

    assert "700 hubs rated 78.4 kWh." in response.text
    assert "question screened by claude-haiku-4-5-20251001" in response.text
    assert "no model used" not in response.text and "AI-assisted" not in response.text


def test_an_empty_question_is_answered_without_calling_the_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    async def fake_post(path: str, payload: dict[str, Any], *, remote_user: str | None = None) -> Any:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(copilot, "post_json", fake_post)
    response = _client().post("/og/copilot/ask", data={"question": "   ", "screen": "/og/"})

    assert response.status_code == 200
    assert called is False
