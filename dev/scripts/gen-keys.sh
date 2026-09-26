#!/bin/sh
# dev/scripts/gen-keys.sh -- generates the dev guardian/safestop Ed25519 keypairs into
# dev/keys/ (gitignored) using the orchestrator's own keygen CLIs (BUILD.md: "A keygen CLI
# writes the public key file the fleet simulator uses"), so the dev stack's key format is
# guaranteed identical to production's, not a hand-rolled equivalent.
#
# Idempotent-ish: skips regenerating a keypair that already exists so `make dev-up` doesn't
# invalidate a previous run's keys (and any commands signed against them) on every invocation.
# Pass `--force` to regenerate both keypairs anyway.
set -eu

REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
KEYS_DIR="$REPO_ROOT/dev/keys"
PYTHON="$REPO_ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON=python3
export PYTHONPATH="$REPO_ROOT/orchestrator/src${PYTHONPATH:+:$PYTHONPATH}"

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

mkdir -p "$KEYS_DIR"

if [ "$FORCE" -eq 1 ] || [ ! -f "$KEYS_DIR/guardian-dev.key" ]; then
    "$PYTHON" -m opengrid.guardian keygen --out "$KEYS_DIR" --key-id guardian-dev
else
    echo "gen-keys.sh: $KEYS_DIR/guardian-dev.key already exists, skipping (use --force to regenerate)"
fi

if [ "$FORCE" -eq 1 ] || [ ! -f "$KEYS_DIR/safestop-dev.key" ]; then
    "$PYTHON" -m opengrid.safestop.keys keygen \
        --key-id safestop-dev \
        --key-out "$KEYS_DIR/safestop-dev.key" \
        --pubkey-out "$KEYS_DIR/safestop-dev.pub"
else
    echo "gen-keys.sh: $KEYS_DIR/safestop-dev.key already exists, skipping (use --force to regenerate)"
fi

echo "gen-keys.sh: dev/keys/ ready:"
ls -l "$KEYS_DIR"
