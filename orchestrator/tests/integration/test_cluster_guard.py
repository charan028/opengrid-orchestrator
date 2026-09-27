"""The og_t_* -> port 5433 rule (`cluster_guard`). Pure logic: runs anywhere, no database needed."""

from __future__ import annotations

import pytest

from .cluster_guard import WrongClusterError, require_test_cluster, require_test_cluster_dsn


@pytest.mark.parametrize("port", [5432, "5432", None, "", 5434, 15433])
def test_workspace_database_off_the_test_cluster_is_refused(port: object) -> None:
    with pytest.raises(WrongClusterError, match=r"og_t_ws.*5433"):
        require_test_cluster("og_t_ws", port)  # type: ignore[arg-type]


@pytest.mark.parametrize("port", [5433, "5433"])
def test_workspace_database_on_the_test_cluster_passes(port: object) -> None:
    require_test_cluster("og_t_ws", port)  # type: ignore[arg-type]


@pytest.mark.parametrize("dbname", ["og", "og_test", "postgres", "og_restore_check"])
def test_other_databases_are_not_this_rules_business(dbname: str) -> None:
    require_test_cluster(dbname, 5432)


def test_dsn_form_reads_the_database_and_port_the_connection_would_use() -> None:
    require_test_cluster_dsn("host=127.0.0.1 port=5433 dbname=og_t_ws user=u")
    with pytest.raises(WrongClusterError):
        require_test_cluster_dsn("host=127.0.0.1 port=5432 dbname=og_t_ws_topo user=u")
    with pytest.raises(WrongClusterError):  # no port: libpq's default 5432
        require_test_cluster_dsn("host=127.0.0.1 dbname=og_t_ws_restore_ab12 user=u")
