"""The one rule for workspace databases: an `og_t_*` database lives only on the disposable test cluster
(port 5433), never on the production cluster (5432) or any other port. The R2 incident (2026-09-26) showed
test I/O on 5432 starving production commits, and 31 stray og_t_* databases had to be dropped from 5432 on
2026-09-26/27. Every integration helper that connects to or creates a workspace database calls
`require_test_cluster` / `require_test_cluster_dsn` first; tools/ws_env.sh and deploy/scripts/create_schema.sh
enforce the same rule in shell.
"""

from __future__ import annotations

from psycopg.conninfo import conninfo_to_dict

TEST_CLUSTER_PORT = 5433
WORKSPACE_DB_PREFIX = "og_t_"
DEFAULT_PG_PORT = 5432


class WrongClusterError(RuntimeError):
    """A workspace (`og_t_*`) database was addressed on a port other than the test cluster's."""


def require_test_cluster(dbname: str, port: int | str | None) -> None:
    """Raise `WrongClusterError` when `dbname` is a workspace database and `port` is not 5433. A missing port
    means libpq's default, 5432, which is refused too. Any other database name is not this rule's business."""
    if not dbname.startswith(WORKSPACE_DB_PREFIX):
        return
    effective = int(port) if port not in (None, "") else DEFAULT_PG_PORT
    if effective != TEST_CLUSTER_PORT:
        raise WrongClusterError(
            f"refusing workspace database {dbname!r} on port {effective}: og_t_* databases live only on the "
            f"test cluster, port {TEST_CLUSTER_PORT} (set OG_DB_PORT=PGPORT={TEST_CLUSTER_PORT}, or run through "
            f"tools/remote.ps1, which sources tools/ws_env.sh)"
        )


def require_test_cluster_dsn(dsn: str) -> None:
    """`require_test_cluster` for a libpq DSN (the database and port the connection would actually use)."""
    params = conninfo_to_dict(dsn)
    require_test_cluster(str(params.get("dbname", "")), params.get("port"))
