from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from opengrid.core.crypto import generate_keypair
from opengrid.platform.config import Config
from opengrid.safestop.keys import (
    DEFAULT_SEED_ENV_VAR,
    SafestopKeyError,
    _cli,
    load_signing_key,
    load_signing_key_from_env,
    load_signing_key_from_file,
)


def test_load_signing_key_from_file_raw_bytes(tmp_path: Path):
    seed, pub = generate_keypair()
    key_path = tmp_path / "safestop.key"
    key_path.write_bytes(seed)

    key = load_signing_key_from_file("safestop-2026a", key_path)
    assert key.key_id == "safestop-2026a"
    assert key.seed == seed
    assert key.public_key_hex() == pub.hex()


def test_load_signing_key_from_file_hex_text(tmp_path: Path):
    seed, _pub = generate_keypair()
    key_path = tmp_path / "safestop.key"
    key_path.write_text(seed.hex(), encoding="ascii")

    key = load_signing_key_from_file("safestop-2026a", key_path)
    assert key.seed == seed


def test_load_signing_key_from_file_missing_raises(tmp_path: Path):
    with pytest.raises(SafestopKeyError):
        load_signing_key_from_file("safestop-2026a", tmp_path / "does-not-exist.key")


def test_load_signing_key_from_file_wrong_length_raises(tmp_path: Path):
    key_path = tmp_path / "safestop.key"
    key_path.write_bytes(b"too-short")
    with pytest.raises(SafestopKeyError):
        load_signing_key_from_file("safestop-2026a", key_path)


def test_load_signing_key_from_env_missing_raises(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(DEFAULT_SEED_ENV_VAR, raising=False)
    with pytest.raises(SafestopKeyError):
        load_signing_key_from_env("safestop-2026a")


def test_load_signing_key_from_env_valid_hex(monkeypatch: pytest.MonkeyPatch):
    seed, _pub = generate_keypair()
    monkeypatch.setenv(DEFAULT_SEED_ENV_VAR, seed.hex())
    key = load_signing_key_from_env("safestop-2026a")
    assert key.seed == seed


def test_load_signing_key_from_env_bad_hex_raises(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(DEFAULT_SEED_ENV_VAR, "not-hex-zzz")
    with pytest.raises(SafestopKeyError):
        load_signing_key_from_env("safestop-2026a")


def test_load_signing_key_prefers_config_key_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    seed, _pub = generate_keypair()
    key_path = tmp_path / "safestop.key"
    key_path.write_bytes(seed)
    cfg = Config({"safestop": {"key_path": str(key_path)}})

    other_seed, _ = generate_keypair()
    monkeypatch.setenv(DEFAULT_SEED_ENV_VAR, other_seed.hex())

    key = load_signing_key("safestop-2026a", cfg)
    assert key.seed == seed  # key_path wins over the env var when both are present


def test_load_signing_key_falls_back_to_env_without_key_path(monkeypatch: pytest.MonkeyPatch):
    seed, _pub = generate_keypair()
    monkeypatch.setenv(DEFAULT_SEED_ENV_VAR, seed.hex())
    cfg = Config({"safestop": {}})

    key = load_signing_key("safestop-2026a", cfg)
    assert key.seed == seed


def test_seed_from_bytes_rejects_wrong_length_hex(tmp_path: Path):
    key_path = tmp_path / "safestop.key"
    key_path.write_text("deadbeef", encoding="ascii")  # valid hex, wrong length
    with pytest.raises(SafestopKeyError):
        load_signing_key_from_file("safestop-2026a", key_path)


def test_cli_keygen_in_process(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    key_out = tmp_path / "safestop.key"
    pubkey_out = tmp_path / "safestop-pub.txt"

    rc = _cli(
        [
            "keygen",
            "--key-id",
            "safestop-2026a",
            "--key-out",
            str(key_out),
            "--pubkey-out",
            str(pubkey_out),
        ]
    )

    assert rc == 0
    assert len(key_out.read_bytes()) == 32
    # Bug fix (dispatch-live pass): the pubkey file is bare hex, no "<key_id> " prefix -- ogsim's
    # loader (ogsim.common.crypto.load_public_key) only accepts a plain 64-char hex/base64 string.
    pubkey_text = pubkey_out.read_text(encoding="utf-8").strip()
    assert len(pubkey_text) == 64
    bytes.fromhex(pubkey_text)  # raises ValueError if not valid hex
    out = capsys.readouterr().out
    assert "key_id=safestop-2026a" in out
    assert "seed_hex=" in out

    # the file written by keygen loads back correctly
    loaded = load_signing_key_from_file("safestop-2026a", key_out)
    assert loaded.public_key_hex() in pubkey_out.read_text(encoding="utf-8")


def test_keygen_cli_writes_private_and_public_key_files(tmp_path: Path):
    key_out = tmp_path / "safestop.key"
    pubkey_out = tmp_path / "safestop-pub.txt"
    src_root = Path(__file__).resolve().parents[3] / "src"
    env = {**os.environ, "PYTHONPATH": str(src_root)}

    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell, test-only
        [
            sys.executable,
            "-m",
            "opengrid.safestop.keys",
            "keygen",
            "--key-id",
            "safestop-2026a",
            "--key-out",
            str(key_out),
            "--pubkey-out",
            str(pubkey_out),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert key_out.read_bytes()
    assert len(key_out.read_bytes()) == 32
    pubkey_text = pubkey_out.read_text(encoding="utf-8").strip()
    assert len(pubkey_text) == 64
    bytes.fromhex(pubkey_text)  # raises ValueError if not valid hex
    assert "seed_hex=" not in pubkey_text  # the seed never lands in the public-key file
