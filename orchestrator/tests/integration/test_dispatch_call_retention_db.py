"""Retention of the dispatch-call audit trail (D-33): og.dispatch_call and og.as_deployment are listed in
og.data_retention (migration 0052) as protected, never deleted (mode NONE), like the other settlement/audit tables. Disposable
test cluster only (5433; OG_DB/OG_DB_PORT set by the runner)."""

from __future__ import annotations

import os

import psycopg
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("OG_DB"), reason="requires the server environment (the 5433 test cluster)"
)


@pytest.fixture(scope="module")
def dsn(server_config, _migrated) -> str:  # type: ignore[no-untyped-def]
    from opengrid.platform.db import build_dsn

    return str(build_dsn(server_config))


def test_ts_d33_it_04_call_audit_tables_are_protected_never_deleted(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT table_name, ts_column, mode, keep_days, protected FROM og.data_retention "
            "WHERE table_name IN ('dispatch_call', 'as_deployment') ORDER BY table_name"
        ).fetchall()
    assert rows == [
        ("as_deployment", "created_at", "NONE", None, True),
        ("dispatch_call", "created_at", "NONE", None, True),
    ]


def test_ts_d33_it_05_a_protected_call_table_cannot_be_given_a_keep_window(dsn: str) -> None:
    with psycopg.connect(dsn) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "UPDATE og.data_retention SET keep_days = 30, mode = 'DELETE' WHERE table_name = 'dispatch_call'"
        )
