"""Fixtures for `opengrid.calls`: an in-memory call store and a recording trace store."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest

from .fakes import FakeCallStore


@dataclass(frozen=True)
class _Ref:
    trace_id: UUID


@dataclass
class RecordingTrace:
    """Duck-typed `TraceStore`: records every append (the real hash chain is tested in opengrid.trace)."""

    events: list[dict[str, Any]] = field(default_factory=list)

    async def append(
        self,
        stream_id: str,
        decision_type: str,
        event_class: str,
        payload: dict[str, Any],
        reason_codes: list[str] | None = None,
    ) -> _Ref:
        ref = _Ref(uuid4())
        self.events.append(
            {
                "stream_id": stream_id,
                "decision_type": decision_type,
                "event_class": event_class,
                "payload": payload,
                "reason_codes": reason_codes,
                "trace_id": ref.trace_id,
            }
        )
        return ref

    def classes(self) -> list[str]:
        return [e["event_class"] for e in self.events]


@pytest.fixture
def store() -> FakeCallStore:
    return FakeCallStore()


@pytest.fixture
def trace() -> RecordingTrace:
    return RecordingTrace()
