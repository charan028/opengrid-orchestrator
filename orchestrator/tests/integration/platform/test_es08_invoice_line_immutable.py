"""ES08 (traceability-gap closure, issue #14 item 2): migration 0024 makes `og.invoice_line` insert-only
at the database layer. UPDATE, DELETE and TRUNCATE are rejected (SQLSTATE 42501) for the app role;
INSERT, including a correcting row that `supersedes` the original, still works; and only the explicit
migration escape hatch (`SET LOCAL og.allow_invoice_line_mutation = 'on'`) lets a mutation through.

DB-only (BUILD.md S6): run via
`powershell -File tools\\remote.ps1 -Ws <ws> -Cmd "cd orchestrator && python -m pytest tests/integration/platform/test_es08_invoice_line_immutable.py -q"`.
Skipped automatically when no database is reachable. Every row this test writes is rolled back at the
end, so it leaves nothing behind in the workspace database.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import UUID, uuid4

import psycopg
import pytest

from opengrid.platform.config import load_config
from opengrid.platform.db import build_dsn, migrate_sync

_CONFIG_PATH = os.environ.get(
    "OG_CONFIG", str((__file__.rsplit("orchestrator", 1)[0]) + "orchestrator/config/test.toml")
)


def _dsn() -> str | None:
    try:
        return build_dsn(load_config(_CONFIG_PATH))
    except Exception:
        return None


def _db_reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=2):
            return True
    except Exception:
        return False


_DSN = _dsn()
requires_db = pytest.mark.skipif(
    _DSN is None or not _db_reachable(_DSN),
    reason="Postgres not reachable locally; run via tools/remote.ps1 -Ws <ws> (BUILD.md S6).",
)
pytestmark = requires_db


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    """A migrated connection in one open transaction that is rolled back on teardown."""
    assert _DSN is not None
    migrate_sync(_DSN)
    with psycopg.connect(_DSN, autocommit=False) as connection:
        yield connection
        connection.rollback()


def _insert_invoice_line(cur: psycopg.Cursor, *, supersedes: UUID | None = None) -> UUID:
    """Insert a contract -> opportunity -> obligation chain and one invoice line; return the line id."""
    contract_id, opportunity_id, obligation_id, line_id = uuid4(), uuid4(), uuid4(), uuid4()
    cur.execute(
        """INSERT INTO og.contract
            (contract_id, customer_id, service_type, tier, profile_ref, start_at,
             penalty_alpha, penalty_beta, penalty_theta, degradation_cost)
           VALUES (%s, %s, 'DIST_DEFERRAL', 'T1', 'es08-profile@1', now(), 0.01, 0.5, 0.05, 0.03)""",
        (contract_id, uuid4()),
    )
    cur.execute(
        """INSERT INTO og.opportunity
            (opportunity_id, contract_id, window_start, window_end, requested_kw, value_per_mwh)
           VALUES (%s, %s, now() - interval '1 hour', now() + interval '1 hour', 4, 100.0)""",
        (opportunity_id, contract_id),
    )
    cur.execute(
        """INSERT INTO og.obligation
            (obligation_id, opportunity_id, contract_id, service_type, tier, window_start,
             window_end, committed_qty_kw, state)
           VALUES (%s, %s, %s, 'DIST_DEFERRAL', 'T1', now() - interval '1 hour',
                   now() + interval '1 hour', 4, 'DELIVERING')""",
        (obligation_id, opportunity_id, contract_id),
    )
    cur.execute(
        """INSERT INTO og.invoice_line
            (invoice_line_id, contract_id, obligation_id, period_start, period_end, line_type, amount,
             supersedes)
           VALUES (%s, %s, %s, current_date, current_date, 'CAPACITY_PAYMENT', 12.5, %s)""",
        (line_id, contract_id, obligation_id, supersedes),
    )
    return line_id


def _amount(cur: psycopg.Cursor, line_id: UUID) -> object:
    cur.execute("SELECT amount FROM og.invoice_line WHERE invoice_line_id = %s", (line_id,))
    row = cur.fetchone()
    assert row is not None
    return row[0]


def test_es08_update_of_an_invoice_line_is_rejected(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        line_id = _insert_invoice_line(cur)
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="insert-only"), conn.transaction():
            cur.execute("UPDATE og.invoice_line SET amount = 0 WHERE invoice_line_id = %s", (line_id,))
        assert _amount(cur, line_id) == 12.5


def test_es08_delete_of_an_invoice_line_is_rejected(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        line_id = _insert_invoice_line(cur)
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="insert-only"), conn.transaction():
            cur.execute("DELETE FROM og.invoice_line WHERE invoice_line_id = %s", (line_id,))
        assert _amount(cur, line_id) == 12.5


def test_es08_mutation_matching_no_rows_is_still_rejected(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            cur.execute("UPDATE og.invoice_line SET amount = 0 WHERE false")
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            cur.execute("DELETE FROM og.invoice_line WHERE false")


def test_es08_truncate_of_invoice_line_is_rejected(conn: psycopg.Connection) -> None:
    with (
        conn.cursor() as cur,
        pytest.raises(psycopg.errors.InsufficientPrivilege, match="TRUNCATE"),
        conn.transaction(),
    ):
        # og.invoice_dispute references invoice_line, so a bare TRUNCATE fails on the FK before any
        # trigger; name both tables so the statement reaches, and is refused by, the ES08 trigger.
        cur.execute("TRUNCATE og.invoice_line, og.invoice_dispute")


def test_es08_correcting_insert_with_supersedes_is_still_allowed(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        original = _insert_invoice_line(cur)
        correction = _insert_invoice_line(cur, supersedes=original)
        cur.execute("SELECT supersedes FROM og.invoice_line WHERE invoice_line_id = %s", (correction,))
        assert cur.fetchone() == (original,)


def test_es08_migration_escape_hatch_allows_a_mutation_in_its_own_transaction(
    conn: psycopg.Connection,
) -> None:
    with conn.cursor() as cur:
        line_id = _insert_invoice_line(cur)
        cur.execute("SET LOCAL og.allow_invoice_line_mutation = 'on'")
        cur.execute("UPDATE og.invoice_line SET amount = 1 WHERE invoice_line_id = %s", (line_id,))
        assert _amount(cur, line_id) == 1
        conn.rollback()

        # SET LOCAL ends with its transaction: the next transaction on the same session is guarded again.
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            cur.execute("DELETE FROM og.invoice_line WHERE false")
