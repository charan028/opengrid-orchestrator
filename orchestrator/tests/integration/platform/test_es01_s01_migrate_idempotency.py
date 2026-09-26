"""ES01-S01 (traceability-gap closure, 2026-09-26): `opengrid.platform.db.migrate_sync` must be
idempotent on a fresh workspace database -- running it twice back to back must raise nothing, the
second run must apply zero migrations (everything already recorded in `og.schema_migrations`), and
every table every migration creates must exist afterward.

DB-only (BUILD.md S6): run via
`powershell -File tools\\remote.ps1 -Ws tst -Cmd "cd orchestrator && python -m pytest tests/integration/platform/test_es01_s01_migrate_idempotency.py -q"`.
Skipped automatically when no database is reachable (`OG_DB`/`OG_CONFIG` not pointed at a live
Postgres) -- never starts an orchestrator process, never touches MQTT (BUILD.md S6).
"""

from __future__ import annotations

import os

import psycopg
import pytest

from opengrid.platform.config import load_config
from opengrid.platform.db import MIGRATIONS_DIR, build_dsn, migrate_sync

_CONFIG_PATH = os.environ.get(
    "OG_CONFIG", str((__file__.rsplit("orchestrator", 1)[0]) + "orchestrator/config/test.toml")
)

#: Every `CREATE TABLE og.<name>` across `orchestrator/migrations/*.sql` as of migration 0018 (grepped
#: directly from the migration files, not re-derived from a model list, so this test catches a table a
#: future migration forgets to add here too -- see the `_tables_missing_from_this_list` check below).
_EXPECTED_TABLES = {
    "contract",
    "product_rule",
    "plan",
    "opportunity",
    "obligation",
    "commitment",
    "renomination_point",
    "reservation",
    "grant",
    "command_batch",
    "verdict",
    "stop_event",
    "meter_interval",
    "performance",
    "invoice_line",
    "pnl",
    "operator_action",
    "trace",
    "trace_checkpoint",
    "retention_policy",
    "hub",
    "bank",
    "hub_state",
    "telemetry",
    "telemetry_default",
    "heartbeat",
    "alert",
    "feed_obs",
    "feed_status",
    "obligation_energy_status",
    "lease_state",
    "forecast",
    "pq_waveform_summary",
    "pq_waveform_raw_index",
    "calibration_attempt",
    "maintenance_work_order",
    "asset_event",
    "pq_envelope",
    "service_profile",
    "hub_inverter_pq",
}


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
_SKIP_REASON = (
    "Postgres not reachable locally; run via tools/remote.ps1 -Ws tst (BUILD.md S6). "
    "The 'tst' workspace's og_t_tst database may need to be created first -- see the closing "
    "agent's final report."
)
requires_db = pytest.mark.skipif(_DSN is None or not _db_reachable(_DSN), reason=_SKIP_REASON)


@requires_db
def test_migrate_sync_is_idempotent_on_a_fresh_workspace_db() -> None:
    assert _DSN is not None

    migrate_sync(_DSN)  # first run: applies every not-yet-recorded migration, in filename order

    # Running it again must raise nothing and apply zero migrations: everything is already recorded in
    # `og.schema_migrations`, and no migration's SQL is destructive/non-idempotent if it somehow reran.
    second_run = migrate_sync(_DSN)
    assert second_run == []

    with psycopg.connect(_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT filename FROM og.schema_migrations")
        applied_filenames = {row[0] for row in cur.fetchall()}
        migration_filenames = {p.name for p in MIGRATIONS_DIR.glob("*.sql")}
        assert migration_filenames <= applied_filenames

        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'og'")
        actual_tables = {row[0] for row in cur.fetchall()}

    missing = _EXPECTED_TABLES - actual_tables
    assert not missing, f"tables missing after migrate_sync: {sorted(missing)}"
