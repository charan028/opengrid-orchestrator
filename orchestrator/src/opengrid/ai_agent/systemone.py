"""System One (TypeSafe Jev) client: fast typed judgements. Owner: ui-a/ai.

This is the tier that makes the copilot feel instant. A System One model returns a *typed answer with a
calibrated probability* rather than generated prose, so the console can branch on it in code. Measured
against the live API with this project\'s own state: 175-420 ms for three questions in one call.

Every question the console needs is asked in a single request, because the questions are independent and
batching them is an order of magnitude faster and cheaper than asking separately (TypeSafe\'s own
parallel-questions benchmark). Nothing here generates text, so nothing here can hallucinate prose into
an operator\'s screen.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from opengrid.ai_agent.types import Judgement

logger = logging.getLogger(__name__)

API_URL = "https://api.typesafe.ai/v1/systemone"
API_KEY_ENV = "TYPESAFE_API_KEY"
DEFAULT_MODEL = "jev-latest"


class SystemOneUnavailable(Exception):  # noqa: N818 -- mirrors opengrid.ui.api_client.ApiUnavailable
    """The judgement service could not answer. Always caught: the console falls back, never blocks."""


class SystemOneClient:
    """Thin async client. Holds no state beyond its configuration so it is safe to share per process."""

    def __init__(
        self, *, model: str = DEFAULT_MODEL, timeout_s: float = 6.0, api_key: str | None = None
    ) -> None:
        self._model = model
        self._timeout_s = timeout_s
        self._api_key = api_key if api_key is not None else os.environ.get(API_KEY_ENV, "")

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    @property
    def model(self) -> str:
        return self._model

    async def ask(self, state: Any, questions: dict[str, dict[str, Any]]) -> dict[str, Judgement]:
        """Ask every question in one call and return the typed answers by question id.

        `state` must already be redacted -- this client does not inspect it, by design: redaction is a
        decision the request layer makes once, where it can be tested, not a judgement scattered here.
        """
        if not self._api_key:
            raise SystemOneUnavailable(f"{API_KEY_ENV} is not set")
        payload = {"state": state, "model": self._model, "questions": questions}
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                response = await client.post(
                    API_URL,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as exc:
            raise SystemOneUnavailable(str(exc)) from exc
        return parse_answers(body)


def parse_answers(body: dict[str, Any]) -> dict[str, Judgement]:
    """Map the API's per-type answer shapes onto one `Judgement`.

    A Choice carries the selected option and a probability per option; a Noul carries only the
    probability of yes (there is no separate confidence -- the probability *is* the answer); a Score
    carries the level. Flattening them here keeps the branching in one tested place instead of at every
    call site.
    """
    answers: dict[str, Judgement] = {}
    for key, raw in (body.get("answers") or {}).items():
        if not isinstance(raw, dict):
            continue
        kind = raw.get("type")
        if kind == "choice":
            answers[key] = Judgement(
                value=raw.get("choice"),
                confidence=raw.get("confidence"),
                probabilities=raw.get("probabilities") or {},
            )
        elif kind == "noul":
            answers[key] = Judgement(value=raw.get("noul"), confidence=None)
        elif kind == "score":
            answers[key] = Judgement(
                value=raw.get("score"),
                confidence=raw.get("confidence"),
                probabilities=raw.get("probabilities") or {},
            )
        else:
            answers[key] = Judgement(value=raw.get("value"))
    return answers


def usage_tokens(body: dict[str, Any]) -> int:
    usage = body.get("usage") or {}
    return int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
