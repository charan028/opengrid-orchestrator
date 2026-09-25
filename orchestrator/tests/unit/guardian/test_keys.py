"""Signing-key resolution (env vs. key file, hex vs. base64) and the `keygen` CLI."""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path

import pytest

from opengrid.core.crypto import private_key_from_seed
from opengrid.guardian import keys

_ENV_VAR = "GUARDIAN_TEST_SEED"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(_ENV_VAR, raising=False)


def test_resolve_seed_from_env_hex(monkeypatch):
    seed = bytes(range(32))
    monkeypatch.setenv(_ENV_VAR, seed.hex())
    resolved = keys.resolve_signing_seed(env_var_name=_ENV_VAR, key_path=None)
    assert resolved == seed


def test_resolve_seed_from_env_base64(monkeypatch):
    seed = bytes(range(32))
    monkeypatch.setenv(_ENV_VAR, base64.b64encode(seed).decode("ascii"))
    resolved = keys.resolve_signing_seed(env_var_name=_ENV_VAR, key_path=None)
    assert resolved == seed


def test_resolve_seed_env_wins_over_key_path(monkeypatch, tmp_path):
    env_seed = bytes(range(32))
    file_seed = bytes(range(32, 64))
    monkeypatch.setenv(_ENV_VAR, env_seed.hex())
    key_file = tmp_path / "guardian.key"
    key_file.write_bytes(file_seed)
    resolved = keys.resolve_signing_seed(env_var_name=_ENV_VAR, key_path=str(key_file))
    assert resolved == env_seed


def test_resolve_seed_from_raw_key_file(tmp_path):
    seed = bytes(range(32))
    key_file = tmp_path / "guardian.key"
    key_file.write_bytes(seed)
    resolved = keys.resolve_signing_seed(env_var_name=_ENV_VAR, key_path=str(key_file))
    assert resolved == seed


def test_resolve_seed_missing_raises():
    with pytest.raises(keys.KeyResolutionError):
        keys.resolve_signing_seed(env_var_name=_ENV_VAR, key_path="/nonexistent/path.key")


def test_resolve_seed_malformed_env_raises(monkeypatch):
    monkeypatch.setenv(_ENV_VAR, "not-hex-or-base64!!")
    with pytest.raises(keys.KeyResolutionError):
        keys.resolve_signing_seed(env_var_name=_ENV_VAR, key_path=None)


def test_keygen_writes_private_and_public_key(tmp_path):
    private_path, public_path = keys.keygen(str(tmp_path), key_id="guardian-test")

    assert private_path.exists()
    assert public_path.exists()
    assert private_path.name == "guardian-test.key"
    assert public_path.name == "guardian-test.pub"

    seed = private_path.read_bytes()
    assert len(seed) == 32
    public_hex = public_path.read_text(encoding="ascii").strip()
    expected_pub = private_key_from_seed(seed).public_key().public_bytes_raw().hex()
    assert public_hex == expected_pub


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file-mode bits are not meaningful on Windows")
def test_keygen_private_key_is_owner_only(tmp_path):
    private_path, _public_path = keys.keygen(str(tmp_path))
    mode = oct(private_path.stat().st_mode)[-3:]
    assert mode == "600"


def test_cli_keygen_direct_call(tmp_path):
    from opengrid.guardian.__main__ import _cli

    out_dir = tmp_path / "direct"
    rc = _cli(["keygen", "--out", str(out_dir), "--key-id", "guardian-direct"])
    assert rc == 0
    assert (out_dir / "guardian-direct.key").exists()
    assert (out_dir / "guardian-direct.pub").exists()


def test_keygen_cli_via_module(tmp_path):
    src_dir = Path(__file__).resolve().parents[3] / "src"
    out_dir = tmp_path / "keys"
    env = {**os.environ, "PYTHONPATH": str(src_dir)}
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell, test-only
        [sys.executable, "-m", "opengrid.guardian", "keygen", "--out", str(out_dir)],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert (out_dir / "guardian-2026a.key").exists()
    assert (out_dir / "guardian-2026a.pub").exists()
