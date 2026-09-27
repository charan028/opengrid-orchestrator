"""opengrid.health.queries.write_hub_health_batch (defect fix, dispatch-live pass): proves the health
evaluator's hub-health write is one batched `UPDATE ... FROM (VALUES ...)` statement under asynchronous
commit, chunked at `_WRITE_HUB_HEALTH_CHUNK_SIZE` rows -- not one single-row `UPDATE`+commit per hub.
Minimal fake pool/cursor, no real DB (BUILD.md S5), mirroring tests/unit/fleet/test_pg_backend.py's
pattern."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from opengrid.health import queries

NOW = datetime(2026, 9, 26, 0, 5, 0, tzinfo=UTC)
SINCE = datetime(2026, 9, 26, 0, 0, 0, tzinfo=UTC)

pytestmark = pytest.mark.asyncio


class FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, object]] = []

    async def execute(self, sql, params=None):
        # Preserve the params' own shape: a dict (named placeholders, e.g. INSERT/DELETE here) stays a
        # dict; a list/tuple (positional placeholders, e.g. the batched hub-health UPDATE) is copied.
        if params is None:
            recorded: object = []
        elif isinstance(params, dict):
            recorded = dict(params)
        else:
            recorded = list(params)
        self.executed.append((str(sql), recorded))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, cursor: FakeCursor) -> None:
        self._conn = FakeConn(cursor)

    def connection(self):
        return self._conn


async def test_write_hub_health_batch_noop_on_empty_changes() -> None:
    cursor = FakeCursor()
    pool = FakePool(cursor)

    await queries.write_hub_health_batch(pool, [])

    assert cursor.executed == []  # no round trip at all for an unchanged cycle


async def test_write_hub_health_batch_uses_async_commit_and_one_statement() -> None:
    cursor = FakeCursor()
    pool = FakePool(cursor)
    changes = [("hub-1", "offline"), ("hub-2", "stale"), ("hub-3", "fault")]

    await queries.write_hub_health_batch(pool, changes)

    assert cursor.executed[0] == ("SET LOCAL synchronous_commit TO OFF", [])
    assert len(cursor.executed) == 2  # SET LOCAL + exactly one UPDATE statement, no per-row round trips
    statement, params = cursor.executed[1]
    assert "UPDATE og.hub_state" in statement
    assert "FROM (VALUES" in statement
    assert params == ["hub-1", "offline", "hub-2", "stale", "hub-3", "fault"]
    assert pool._conn.committed is True


async def test_write_hub_health_batch_chunks_at_500_rows() -> None:
    """Benchmark-style shape check: 1,200 changed hubs (dispatch-live pass's ~2,000/cycle scale) become
    exactly 3 chunked UPDATE statements (500 + 500 + 200), still one commit for the whole batch."""
    cursor = FakeCursor()
    pool = FakePool(cursor)
    changes = [(f"hub-{i}", "offline") for i in range(1_200)]

    await queries.write_hub_health_batch(pool, changes)

    update_statements = [(s, p) for s, p in cursor.executed if "UPDATE og.hub_state" in s]
    assert len(update_statements) == 3
    assert [len(p) // 2 for _s, p in update_statements] == [500, 500, 200]
    assert pool._conn.committed is True


class FetchingFakeCursor(FakeCursor):
    """Like `FakeCursor`, but `execute` on the configured SELECT returns `rows` from `fetchall`, so
    `write_degraded_modes`'s internal `fetch_degraded_modes` read has something to work with."""

    def __init__(self, rows: list[tuple[str, object]]) -> None:
        super().__init__()
        self._rows = rows

    async def fetchall(self):
        return self._rows


async def test_write_degraded_modes_noop_when_nothing_changed() -> None:
    """Defect fix (R2): `evaluate_once` now persists degraded modes every cycle -- when the active set
    already matches what's stored, this must be a read-only no-op (no INSERT/DELETE/commit)."""
    cursor = FetchingFakeCursor(rows=[("HOLD_LOCAL_AUTONOMY", SINCE)])
    pool = FakePool(cursor)

    await queries.write_degraded_modes(pool, frozenset({"HOLD_LOCAL_AUTONOMY"}), now=NOW)

    assert len(cursor.executed) == 1  # only the SELECT from fetch_degraded_modes
    assert pool._conn.committed is False


async def test_write_degraded_modes_inserts_new_and_deletes_resolved() -> None:
    cursor = FetchingFakeCursor(rows=[("HOLD_LOCAL_AUTONOMY", SINCE)])
    pool = FakePool(cursor)

    await queries.write_degraded_modes(pool, frozenset({"NO_NEW_COMMITMENTS"}), now=NOW)

    statements = [s for s, _p in cursor.executed]
    assert "SET LOCAL synchronous_commit TO OFF" in statements
    inserted = [p for s, p in cursor.executed if "INSERT INTO og.degraded_mode_state" in s]
    deleted = [p for s, p in cursor.executed if "DELETE FROM og.degraded_mode_state" in s]
    assert inserted == [{"mode": "NO_NEW_COMMITMENTS", "since": NOW}]
    assert deleted == [{"mode": "HOLD_LOCAL_AUTONOMY"}]
    assert pool._conn.committed is True


async def test_fetch_degraded_modes_returns_mode_since_pairs() -> None:
    cursor = FetchingFakeCursor(rows=[("HOLD", SINCE)])
    pool = FakePool(cursor)

    result = await queries.fetch_degraded_modes(pool)

    assert result == [("HOLD", SINCE)]


class ReturningFakeCursor(FakeCursor):
    """Like `FakeCursor`, but `fetchone` returns a fixed row -- for `raise_alert`'s `RETURNING id`."""

    def __init__(self, fetchone_row: tuple) -> None:
        super().__init__()
        self._row = fetchone_row

    async def fetchone(self):
        return self._row


async def test_raise_alert_populates_scope_columns_from_detail() -> None:
    """R2 item 1: `og.alert.scope_kind`/`scope_ref` (migration 0024) come straight from
    `AlertFinding.detail`'s "scope_kind"/"scope_ref" keys -- the single writer both health's own rules
    and guardian's `PgAlertPort.raise_alert` (`opengrid.guardian.repo`) go through, so guardian's
    escalation alerts (whose `detail` already carries these keys, `opengrid.guardian.main.
    apply_escalation`) get structured scope with no guardian-side change."""
    from opengrid.health.model import AlertFinding

    cursor = ReturningFakeCursor(fetchone_row=(42,))
    pool = FakePool(cursor)
    finding = AlertFinding(
        rule="ALR-SCOPE-CONSERVATIVE",
        severity="warning",
        summary="BANK:bank-000 CONSERVATIVE: 12% of commands vetoed",
        condition_key="ALR-SCOPE-CONSERVATIVE:BANK:bank-000",
        detail={"scope_kind": "BANK", "scope_ref": "bank-000", "veto_ratio": 0.12},
    )

    alert_id = await queries.raise_alert(pool, finding, opened_at=NOW)

    assert alert_id == 42
    insert_statement, params = cursor.executed[0]
    assert "INSERT INTO og.alert" in insert_statement
    assert params["scope_kind"] == "BANK"
    assert params["scope_ref"] == "bank-000"


async def test_raise_alert_scope_columns_none_when_detail_has_no_scope() -> None:
    """A system-wide alert (e.g. ALR-RESERVE-BREACH) has no scope in its detail -- the columns must be
    `None`, not raise a `KeyError`."""
    from opengrid.health.model import AlertFinding

    cursor = ReturningFakeCursor(fetchone_row=(43,))
    pool = FakePool(cursor)
    finding = AlertFinding(
        rule="ALR-RESERVE-BREACH",
        severity="critical",
        summary="Reserve-floor breach counter at 1 (must be 0, A10)",
        condition_key="ALR-RESERVE-BREACH",
        detail={"count": 1},
    )

    await queries.raise_alert(pool, finding, opened_at=NOW)

    _statement, params = cursor.executed[0]
    assert params["scope_kind"] is None
    assert params["scope_ref"] is None


async def test_fetch_open_alerts_round_trips_scope_columns() -> None:
    cursor = ReturningFakeCursor(fetchone_row=None)

    async def fetchall():
        return [
            (
                1,
                "ALR-SCOPE-CONSERVATIVE",
                "warning",
                "BANK:bank-000 CONSERVATIVE",
                {"scope_kind": "BANK", "scope_ref": "bank-000"},
                NOW,
                None,
                None,
                "BANK",
                "bank-000",
            )
        ]

    cursor.fetchall = fetchall
    pool = FakePool(cursor)

    alerts = await queries.fetch_open_alerts(pool)

    assert len(alerts) == 1
    assert alerts[0].scope_kind == "BANK"
    assert alerts[0].scope_ref == "bank-000"


async def test_fetch_open_alerts_does_not_raise_on_an_open_info_alert() -> None:
    """R3.4.3 fix: `og.alert.severity` is plain TEXT (no CHECK constraint, migration 0001) and firmware
    writes "info"-severity campaign-progress alerts (`opengrid.health.model.AlertSeverity` already
    allows it), but `core.models.platform.Alert.severity`'s Literal was never widened to match, so
    `Alert(**row)`/`Alert(severity=...)` raised a pydantic ValidationError as soon as any info alert was
    open -- breaking every caller of `fetch_open_alerts` (e.g. `engine.alerts.open_alert_details`/
    `clear_open_alerts`, `health.evaluate_alerts`'s own auto-clear pass)."""
    cursor = ReturningFakeCursor(fetchone_row=None)

    async def fetchall():
        return [
            (
                7,
                "ALR-FIRMWARE-CAMPAIGN-PROGRESS",
                "info",
                "Campaign c1: 3/10 hubs updated",
                {"campaign_id": "c1"},
                NOW,
                None,
                None,
                None,
                None,
            )
        ]

    cursor.fetchall = fetchall
    pool = FakePool(cursor)

    alerts = await queries.fetch_open_alerts(pool)

    assert len(alerts) == 1
    assert alerts[0].severity == "info"
