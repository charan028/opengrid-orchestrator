"""Guardian Ed25519 signing-key resolution and the `keygen` CLI (BUILD.md: "A keygen CLI writes the
public key file the fleet simulator uses").

Key material comes from `GUARDIAN_SIGNING_SEED` (env, `secrets.env`, per interfaces/crypto.md S3) if
set, else from the 32-byte raw seed file at `guardian.key_path` (mode 0600). Never logs a resolved seed
value (BUILD.md safety rule) -- only env-var *names* or file *paths* appear in log/error messages.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

from opengrid.core.crypto import generate_keypair, private_key_from_seed

_SEED_LEN = 32


class KeyResolutionError(Exception):
    """Raised when no usable Ed25519 seed can be resolved from env or key file."""


def _decode_seed(raw: str) -> bytes:
    """Accept a 32-byte seed encoded as hex (64 chars) or base64 (any padding)."""
    stripped = raw.strip()
    if len(stripped) == _SEED_LEN * 2:
        try:
            return bytes.fromhex(stripped)
        except ValueError:
            pass
    padded = stripped + "=" * (-len(stripped) % 4)
    try:
        decoded = base64.b64decode(padded, validate=False)
    except (ValueError, TypeError) as exc:
        raise KeyResolutionError("signing seed is neither valid hex nor valid base64") from exc
    if len(decoded) != _SEED_LEN:
        raise KeyResolutionError(f"decoded signing seed is {len(decoded)} bytes, expected {_SEED_LEN}")
    return decoded


def resolve_signing_seed(*, env_var_name: str, key_path: str | None) -> bytes:
    """Resolve the guardian's private seed: `env_var_name` wins if set, else read `key_path` (raw
    32 bytes, or hex/base64 text). Raises `KeyResolutionError` if neither yields a usable seed --
    guardian must fail to start rather than sign with no key (K3: sole signer)."""
    env_value = os.environ.get(env_var_name)
    if env_value:
        return _decode_seed(env_value)
    if key_path:
        path = Path(key_path)
        if path.exists():
            raw = path.read_bytes()
            if len(raw) == _SEED_LEN:
                return raw
            return _decode_seed(raw.decode("ascii", errors="strict"))
    raise KeyResolutionError(
        f"no guardian signing seed: env var {env_var_name} is unset and key file {key_path!r} is missing"
    )


def keygen(out_dir: str, *, key_id: str = "guardian-2026a") -> tuple[Path, Path]:
    """Generate a fresh Ed25519 keypair; write the private seed (mode 0600) and the public key
    (mode 0644, hex-encoded) under `out_dir`. Returns (private_path, public_path). The public key file
    is what `ogsim`'s fleet simulator loads to verify guardian-signed command batches (interfaces/
    crypto.md S3)."""
    seed, public = generate_keypair()
    # Round-trip sanity check before anything touches disk.
    private_key_from_seed(seed).public_key().public_bytes_raw()

    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)

    private_path = directory / f"{key_id}.key"
    public_path = directory / f"{key_id}.pub"

    private_path.write_bytes(seed)
    os.chmod(private_path, 0o600)

    public_path.write_text(public.hex() + "\n", encoding="ascii")
    os.chmod(public_path, 0o644)

    return private_path, public_path
