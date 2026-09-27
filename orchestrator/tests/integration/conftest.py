"""Integration test fixtures: real Postgres + MQTT, only runnable on the server via
`tools/remote.ps1 -Ws <workspace>` (BUILD.md S5), which sets `OG_DB`/`OG_MQTT_ROOT` and the DB/MQTT
password env vars. Every test module under `tests/integration` is skipped entirely when `OG_DB` is not
set, so `pytest tests/unit` (the local, no-DB/MQTT command) never touches this file's fixtures.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from .cluster_guard import require_test_cluster

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

REQUIRES_SERVER_ENV = pytest.mark.skipif(
    not os.environ.get("OG_DB"),
    reason="integration tests require the server environment (tools/remote.ps1 sets OG_DB/OG_MQTT_ROOT)",
)


@pytest.fixture(scope="session")
def server_config():
    from opengrid.platform.config import Config

    data = {
        "postgres": {
            "host": os.environ.get("PGHOST", "127.0.0.1"),
            "port": int(os.environ.get("PGPORT", "5432")),
            "database": os.environ["OG_DB"],
            "pool_min": 1,
            "pool_max": 4,
        },
        "mqtt": {
            "host": os.environ.get("MQTT_HOST", "127.0.0.1"),
            "port": int(os.environ.get("MQTT_PORT", "1883")),
            "topic_root": os.environ["OG_MQTT_ROOT"],
            "keepalive_s": 20,
        },
        "safestop": {"confirm_window_s": 30},
    }
    # build_dsn's port: OG_DB_PORT first, then postgres.port. og_t_* only on 5433 (cluster_guard).
    require_test_cluster(
        data["postgres"]["database"], os.environ.get("OG_DB_PORT") or data["postgres"]["port"]
    )
    return Config(data)


@pytest.fixture(scope="session")
def _migrated(server_config):
    from opengrid.platform.db import build_dsn, migrate_sync

    migrate_sync(build_dsn(server_config))
    return True
