"""deploy/scripts/create_schema.sh refuses an og_t_* database on any port but the test cluster's (5433),
before doing anything, --dry-run included. Runs the real script under bash with --dry-run (no database, no
secrets written: the dry run only reports). Skipped where bash is unavailable (the Windows dev box)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[4] / "deploy" / "scripts" / "create_schema.sh"
_BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    _BASH is None or not Path("/bin/bash").exists(), reason="needs a POSIX bash (runs on the server)"
)


def _run(tmp_path: Path, db: str, port: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed repo script, test-controlled arguments
        [
            _BASH or "bash",
            str(SCRIPT),
            "--dry-run",
            "--etc",
            str(tmp_path),
            "--db-name",
            db,
            "--db-port",
            port,
        ],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin"},
    )


@pytest.mark.parametrize("port", ["5432", "5434"])
def test_workspace_database_off_the_test_cluster_is_refused(tmp_path: Path, port: str) -> None:
    result = _run(tmp_path, "og_t_guard", port)
    assert result.returncode != 0
    assert f"refusing to create workspace database og_t_guard on port {port}" in result.stderr
    assert "port 5433" in result.stderr
    assert "create_schema:" not in result.stdout  # refused before any step, dry run included
    assert not any(tmp_path.iterdir())


def test_workspace_database_on_the_test_cluster_passes_the_guard(tmp_path: Path) -> None:
    result = _run(tmp_path, "og_t_guard", "5433")
    assert result.returncode == 0, result.stderr
    assert "DRY RUN" in result.stdout


def test_production_database_name_is_not_this_rules_business(tmp_path: Path) -> None:
    result = _run(tmp_path, "og", "5432")
    assert result.returncode == 0, result.stderr
