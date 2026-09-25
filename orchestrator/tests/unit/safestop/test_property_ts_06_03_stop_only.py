"""TS-06-03 (K8 property): safe-stop is stop-only. Randomize stop/attempted-release sequences and
assert no sequence of inputs to `og-safestop` ever produces a RELEASE -- only ENGAGE rows are ever
written, and every `release()` call raises.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from opengrid.core.crypto import generate_keypair
from opengrid.safestop.keys import StopSigningKey
from opengrid.safestop.service import ReleaseNotPermittedError, SafestopService


@dataclass
class _RecordingBackend:
    rows: list[dict[str, Any]] = field(default_factory=list)

    async def insert_stop_event(self, **kwargs: Any) -> None:
        self.rows.append(kwargs)

    async def latest_action(self, scope_kind: str, scope_ref: str) -> str | None:
        return None


@dataclass
class _NullPublisher:
    async def publish_retained(self, topic_suffix: str, payload: dict[str, Any]) -> None:
        return None


_scope_strategy = st.sampled_from(["FLEET", "ZONE", "BANK"])
_action_strategy = st.sampled_from(["engage", "release"])
_word = st.text(min_size=1, max_size=12, alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd")))

_sequence_strategy = st.lists(
    st.tuples(_action_strategy, _scope_strategy, _word, _word),
    min_size=0,
    max_size=25,
)


async def _run_sequence(sequence: list[tuple[str, str, str, str]]) -> list[dict[str, Any]]:
    seed, _pub = generate_keypair()
    backend = _RecordingBackend()
    svc = SafestopService(StopSigningKey("safestop-prop", seed), backend, _NullPublisher(), trace=None)

    for action, scope, ref, actor in sequence:
        scope_ref = "" if scope == "FLEET" else ref
        if action == "engage":
            await svc.engage(scope, scope_ref, "hypothesis drill", f"operator:{actor}")
        else:
            try:
                await svc.release(scope, scope_ref, f"approver:{actor}")
            except ReleaseNotPermittedError:
                pass
            else:
                raise AssertionError("release() must always raise ReleaseNotPermittedError")
    return backend.rows


@given(sequence=_sequence_strategy)
@settings(max_examples=500)
def test_no_sequence_ever_produces_a_release_row(sequence):
    rows = asyncio.run(_run_sequence(sequence))
    assert all(row["action"] == "ENGAGE" for row in rows)
