"""K8 stop authority (00-invariants.md K8): a stop is traced, persisted, then broadcast, and never releases."""

from __future__ import annotations

from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from opengrid.safestop.keys import StopSigningKey
from opengrid.safestop.service import ReleaseNotPermittedError, SafestopService

from .support import Signer, run

_word = st.text(min_size=1, max_size=10, alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd")))
_scopes = st.sampled_from(["FLEET", "ZONE", "BANK"])


class _Journal:
    """Records the order of side effects across the trace, the stop_event table and the broker."""

    def __init__(self) -> None:
        self.steps: list[str] = []
        self.rows: list[dict[str, Any]] = []

    async def append(self, stream_id, decision_type, event_class, payload, reason_codes=None) -> None:
        self.steps.append("trace")

    async def insert_stop_event(self, **row: Any) -> None:
        self.steps.append("persist")
        self.rows.append(row)

    async def latest_action(self, scope_kind: str, scope_ref: str) -> str | None:
        return None

    async def publish_retained(self, topic_suffix: str, payload: dict[str, Any]) -> None:
        self.steps.append("publish")


def _service(journal: _Journal) -> SafestopService:
    return SafestopService(
        StopSigningKey("safestop-prop", Signer.new().seed), journal, journal, trace=journal
    )


@given(_scopes, _word, _word)
def test_k08_an_engage_is_traced_then_persisted_then_published_as_an_engage_only(scope, ref, actor):
    journal = _Journal()

    run(_service(journal).engage(scope, "" if scope == "FLEET" else ref, "drill", f"operator:{actor}"))

    assert journal.steps == ["trace", "persist", "publish"]
    assert [row["action"] for row in journal.rows] == ["ENGAGE"]
    assert journal.rows[0]["initiator_kind"] == "SAFESTOP_AUTHORITY"


@given(_scopes, _word, _word)
def test_k08_a_refused_release_leaves_no_trace_row_event_or_broadcast(scope, ref, approver):
    journal = _Journal()

    try:
        run(_service(journal).release(scope, ref, approver))
    except ReleaseNotPermittedError:
        pass
    else:
        raise AssertionError("release must always be refused")

    assert journal.steps == []
