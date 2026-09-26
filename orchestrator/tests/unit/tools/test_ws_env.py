"""tools/ws_env.sh: the environment tools/remote.ps1 gives a workspace run. Production MQTT credentials
are never exported; the workspace's own broker user is. Runs the real script under bash (skipped where
bash is unavailable, e.g. the Windows dev box; it runs on the server)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

WS_ENV = Path(__file__).resolve().parents[4] / "tools" / "ws_env.sh"
_BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    _BASH is None or not Path("/bin/bash").exists(), reason="needs a POSIX bash (runs on the server)"
)

SECRETS = """OG_DB_PASSWORD=db-secret
OG_MQTT_GUARDIAN_PASSWORD=prod-guardian-secret
export OG_MQTT_SIM_PASSWORD=prod-sim-secret
  OG_MQTT_ENGINE_PASSWORD=prod-engine-secret
GUARDIAN_SIGNING_SEED=seed-stays-as-before
"""


def _env_after(
    tmp_path: Path, *, mqtt_env: str | None, inherited: dict[str, str] | None = None
) -> dict[str, str]:
    (tmp_path / "secrets.env").write_text(SECRETS, encoding="utf-8")
    (tmp_path / "api_keys.env").write_text(
        "ERCOT_PUBLIC_API_KEY_PRIMARY=k\nOG_MQTT_API_PASSWORD=prod-api\n", encoding="utf-8"
    )
    (tmp_path / "work" / "guard").mkdir(parents=True)
    if mqtt_env is not None:
        (tmp_path / "work" / "guard" / ".mqtt.env").write_text(mqtt_env, encoding="utf-8")
    script = f'. "{WS_ENV.as_posix()}"; og_ws_env guard "{tmp_path}/secrets.env" "{tmp_path}/api_keys.env" "{tmp_path}/work"; env'
    result = subprocess.run(  # noqa: S603 -- fixed repo script, test-controlled arguments
        [_BASH or "bash", "-c", script],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin", **(inherited or {})},
    )
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def test_workspace_gets_its_own_user_and_no_production_mqtt_credential(tmp_path):
    env = _env_after(
        tmp_path,
        mqtt_env="# c\nOG_MQTT_WS_USER=ogw_guard\nOG_MQTT_WS_PASSWORD=ws-secret\nOG_MQTT_SIM_PASSWORD=smuggled\n",
        inherited={"OG_MQTT_SAFESTOP_PASSWORD": "prod-safestop-secret"},
    )

    assert (env["OG_MQTT_WS_USER"], env["OG_MQTT_WS_PASSWORD"]) == ("ogw_guard", "ws-secret")
    assert not any(v.startswith("prod-") or v == "smuggled" for v in env.values())
    for role in ("ENGINE", "GUARDIAN", "SIM", "API", "SAFESTOP", "SIMCTL"):
        assert env[f"OG_MQTT_{role}_PASSWORD"] == "workspace-uses-OG_MQTT_WS_PASSWORD"
    assert (env["OG_WS"], env["OG_DB"], env["OG_MQTT_ROOT"]) == ("guard", "og_t_guard", "ogtest/guard")
    assert env["OG_DB_PASSWORD"] == "db-secret"  # noqa: S105 -- a test fixture value
    assert env["ERCOT_PUBLIC_API_KEY_PRIMARY"] == "k"


def test_unprovisioned_workspace_gets_no_mqtt_credential_at_all(tmp_path):
    env = _env_after(tmp_path, mqtt_env=None)
    assert not [name for name in env if name.startswith("OG_MQTT_") and name != "OG_MQTT_ROOT"]
