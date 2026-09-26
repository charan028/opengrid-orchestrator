"""The docked copilot panel (issue #26 item 1). Owner: ui-a.

A UI-owned relay, exactly like the safe-stop and manual-command flows: the browser posts to this route,
this route calls `POST /og/api/ai/ask` server-side with the operator's forwarded identity, and renders
the answer as an HTMX fragment. The panel never talks to the agent or to `opengrid.api` directly.

The assistant being unavailable is a normal answer, not an error: this route never returns a non-200 and
never sets the screen's degraded banner, because nothing else in the console may be blocked or slowed by
the agent's absence (UI-DSP-13, a Must verified by test).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from opengrid.ui.api_client import ApiUnavailable, post_json
from opengrid.ui.role import remote_user, role_of
from opengrid.ui.templating import templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/copilot")

_ASK_PATH = "/og/api/ai/ask"
_UNAVAILABLE = "Assistant unavailable -- deterministic controls unaffected."


@router.post("/ask", response_class=HTMLResponse)
async def ask(
    request: Request,
    question: str = Form(default=""),
    screen: str = Form(default=""),
) -> HTMLResponse:
    """Relay one question and render the answer. Viewers may ask: the agent is read-only and advisory."""
    asked = (question or "").strip()
    if not asked:
        return templates.TemplateResponse(
            request,
            "_partials/copilot_answer.html",
            {
                "answer": {
                    "text": "Ask a question about the fleet, the market or a decision.",
                    "tier": "declined",
                },
                "question": asked,
            },
        )
    answer: dict[str, Any]
    try:
        raw = await post_json(
            _ASK_PATH, {"question": asked[:500], "screen": screen or None}, remote_user=remote_user(request)
        )
        answer = raw if isinstance(raw, dict) else {"text": _UNAVAILABLE, "tier": "unavailable"}
    except ApiUnavailable as exc:
        # Not a warning: an assistant that is switched off or unreachable is an expected state here.
        logger.info("copilot ask unavailable: %s", exc)
        answer = {"text": _UNAVAILABLE, "tier": "unavailable"}
    return templates.TemplateResponse(
        request,
        "_partials/copilot_answer.html",
        {"answer": answer, "question": asked, "role": role_of(request)},
    )
