"""Stop-only Ed25519 key handling (02a S6.5, interfaces/crypto.md S3).

The safestop signing key is deliberately distinct from the guardian's key: it may only ever sign
`StopEvent` messages with `action="ENGAGE"` (enforced at the call site in `opengrid.safestop.service`,
never by this module trusting a caller's intent). This module owns loading that key from disk (per
`orchestrator.toml`'s `[safestop].key_path`, matching `[guardian].key_path`'s convention) or from an
env var, and a `keygen` CLI that writes the private key file plus a public-key file the integration
simulators can read.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from opengrid.core.crypto import generate_keypair, private_key_from_seed
from opengrid.platform.config import Config, ConfigError, resolve_secret

DEFAULT_SEED_ENV_VAR = "OG_SAFESTOP_SIGNING_SEED"
DEFAULT_KEY_PATH_CONFIG_KEY = "safestop.key_path"
SEED_LENGTH_BYTES = 32


class SafestopKeyError(Exception):
    """Raised when the stop-only signing key cannot be loaded or is malformed."""


@dataclass(frozen=True, slots=True)
class StopSigningKey:
    """A stop-only Ed25519 keypair, identified on the wire by `key_id` (e.g. `safestop-2026a`)."""

    key_id: str
    seed: bytes  # 32-byte Ed25519 private seed -- never logged, never re-written to disk by this module

    def public_key_hex(self) -> str:
        pub = private_key_from_seed(self.seed).public_key().public_bytes_raw()
        return pub.hex()


def _seed_from_bytes(raw: bytes, *, source: str) -> bytes:
    if len(raw) == SEED_LENGTH_BYTES:
        return raw
    text = raw.decode("ascii", errors="ignore").strip()
    try:
        seed = bytes.fromhex(text)
    except ValueError as exc:
        raise SafestopKeyError(f"{source} is neither {SEED_LENGTH_BYTES} raw bytes nor hex") from exc
    if len(seed) != SEED_LENGTH_BYTES:
        raise SafestopKeyError(f"{source} must decode to {SEED_LENGTH_BYTES} bytes, got {len(seed)}")
    return seed


def load_signing_key_from_file(key_id: str, key_path: str | Path) -> StopSigningKey:
    """Load the stop-only seed from a key file (raw 32 bytes or hex text), e.g. `[safestop].key_path`."""
    path = Path(key_path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SafestopKeyError(f"cannot read safestop key file {path}: {exc}") from exc
    seed = _seed_from_bytes(raw, source=str(path))
    return StopSigningKey(key_id=key_id, seed=seed)


def load_signing_key_from_env(key_id: str, *, env_var: str = DEFAULT_SEED_ENV_VAR) -> StopSigningKey:
    """Load the stop-only seed from `env_var` (hex-encoded, 64 chars = 32 bytes)."""
    try:
        seed_hex = resolve_secret(env_var)
    except ConfigError as exc:
        raise SafestopKeyError(str(exc)) from exc
    seed = _seed_from_bytes(seed_hex.encode("ascii"), source=env_var)
    return StopSigningKey(key_id=key_id, seed=seed)


def load_signing_key(key_id: str, cfg: Config, *, env_var: str = DEFAULT_SEED_ENV_VAR) -> StopSigningKey:
    """Resolve the stop-only key the way `main.py` does: prefer `[safestop].key_path` from `cfg` (the
    convention shared with `[guardian].key_path`); fall back to `env_var` if no key_path is configured.
    """
    key_path = cfg.get(DEFAULT_KEY_PATH_CONFIG_KEY)
    if key_path:
        return load_signing_key_from_file(key_id, key_path)
    return load_signing_key_from_env(key_id, env_var=env_var)


def _cli(argv: list[str]) -> int:
    """`python -m opengrid.safestop.keys keygen --key-id safestop-2026a --key-out <key_path> --pubkey-out <path>`

    Generates a fresh stop-only Ed25519 keypair, writes the private seed to `--key-out` (mode 0600,
    matching `[safestop].key_path`) and the public key (hex) to `--pubkey-out` for the integration
    simulators' verification config. The seed is never printed by default (qa/security-review.md F-05:
    stdout is easy to capture inadvertently -- shell/terminal scrollback, a CI or deploy log, `script`/
    tmux logging) -- pass `--print-seed` to opt into the old behavior for the one legitimate case
    (bootstrapping `OG_SAFESTOP_SIGNING_SEED` in `secrets.env` instead of using `--key-out`'s file),
    matching `opengrid.guardian.keys.keygen`'s existing never-print-by-default behavior.
    """
    parser = argparse.ArgumentParser(prog="python -m opengrid.safestop.keys")
    sub = parser.add_subparsers(dest="command", required=True)
    keygen = sub.add_parser("keygen", help="generate a new stop-only Ed25519 keypair")
    keygen.add_argument("--key-id", required=True, help="e.g. safestop-2026a")
    keygen.add_argument("--key-out", required=True, type=Path, help="file to write the private seed to")
    keygen.add_argument("--pubkey-out", required=True, type=Path, help="file to write the public key hex to")
    keygen.add_argument(
        "--print-seed",
        action="store_true",
        help="print the raw seed hex to stdout (only for bootstrapping OG_SAFESTOP_SIGNING_SEED; "
        "the private key file is already the durable, permissioned artifact -- avoid this flag "
        "outside that one case)",
    )
    args = parser.parse_args(argv)

    seed, pub = generate_keypair()

    args.key_out.parent.mkdir(parents=True, exist_ok=True)
    args.key_out.write_bytes(seed)
    with contextlib.suppress(OSError):  # best-effort chmod on platforms without POSIX permissions
        os.chmod(args.key_out, 0o600)

    # Bug fix (dispatch-live pass): this used to write "<key_id> <hex>\n", but
    # ogsim.common.crypto.load_public_key/_decode_key_text (the only reader of this file) accepts only a
    # bare 64-char hex (or base64) string -- the "<key_id> " prefix made the file unparsable. `key_id` is
    # carried on the wire per-message instead (interfaces/crypto.md S3's `key_id` field on a signed
    # envelope), never inside the key file itself, matching `opengrid.guardian.keys.keygen`'s own
    # plain-hex `.pub` format.
    args.pubkey_out.parent.mkdir(parents=True, exist_ok=True)
    args.pubkey_out.write_text(f"{pub.hex()}\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(args.pubkey_out, 0o644)

    print(f"key_id={args.key_id}")
    print(f"public_key_hex={pub.hex()}")
    print(f"private key written to {args.key_out} (mode 0600)")
    print(f"public key written to {args.pubkey_out}")
    if args.print_seed:
        print(
            f"seed_hex={seed.hex()}  # only if using {DEFAULT_SEED_ENV_VAR} instead of key_path -- "
            "do not log again"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
