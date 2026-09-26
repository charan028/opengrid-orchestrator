"""`opengrid.api.trace_backend` is the canonical `opengrid.trace.pg_backend.PgTraceBackend` (no second copy
of the `og.trace` I/O), so og-api's operator-action traces get the K11 fail-safe journal: a row that
cannot reach Postgres is journaled at `[trace].journal_path`, not lost."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from opengrid.api import trace_backend as api_trace_backend
from opengrid.trace import pg_backend


class _DownPool:
    """A fake `AsyncConnectionPool` whose every `.connection()` fails (a Postgres outage)."""

    def connection(self) -> Any:
        raise psycopg.OperationalError("simulated outage")


class _Cfg:
    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)


def test_api_backend_is_the_canonical_trace_backend() -> None:
    assert api_trace_backend.PgTraceBackend is pg_backend.PgTraceBackend
    assert api_trace_backend.journal_path_from_config is pg_backend.journal_path_from_config


def test_journal_path_resolves_from_trace_config(tmp_path: Path) -> None:
    configured = tmp_path / "trace_journal.jsonl"
    assert (
        api_trace_backend.journal_path_from_config(_Cfg({"trace.journal_path": str(configured)}))
        == configured
    )
    assert api_trace_backend.journal_path_from_config(_Cfg({})) == api_trace_backend.DEFAULT_JOURNAL_PATH


@pytest.mark.asyncio
async def test_api_trace_row_is_journaled_when_database_is_unreachable(tmp_path: Path) -> None:
    journal = tmp_path / "trace_journal.jsonl"
    backend = api_trace_backend.PgTraceBackend(
        _DownPool(),  # type: ignore[arg-type]
        journal_path=api_trace_backend.journal_path_from_config(_Cfg({"trace.journal_path": str(journal)})),
    )

    await backend.insert_trace_row(
        trace_id=uuid4(),
        stream_id="api",
        seq=0,
        decision_type="OPERATOR_ACTION",
        event_class="OPERATOR_ACTION",
        payload={"action": "test"},
        reason_codes=None,
        prev_hash=None,
        record_hash="hash-api-0",
        created_at=datetime(2026, 9, 26, 12, 0, tzinfo=UTC),
    )

    assert journal.exists()
    assert backend.pending_count() == 1
    assert await backend.last_head("api") == (0, "hash-api-0")  # resumes from the journal while down
