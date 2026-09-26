set -euo pipefail
GUARDIAN_PY=/opt/opengrid/venv/bin/python
export PYTHONPATH=/opt/opengrid/current/orchestrator/src

echo "== guardian keygen =="
# /etc/opengrid is root:opengrid mode 750 -- opengrid can read existing files but not create new ones
# (group has r-x, not w), so key *creation* runs as root; ownership is fixed up to opengrid below so
# the og-guardian/og-safestop services (which run as opengrid, per their systemd units) can read them.
"$GUARDIAN_PY" -m opengrid.guardian keygen --out /etc/opengrid --key-id guardian_ed25519

echo "== safestop keygen =="
"$GUARDIAN_PY" -m opengrid.safestop.keys keygen \
  --key-id safestop-2026a \
  --key-out /etc/opengrid/safestop_ed25519.key \
  --pubkey-out /tmp/safestop_raw.pub > /tmp/safestop_keygen.out
cat /tmp/safestop_keygen.out

# opengrid.safestop.keys' keygen CLI writes "<key_id> <hex>" to --pubkey-out, but
# ogsim.common.crypto.load_public_key expects exactly 64 hex chars (or base64) with no key_id prefix
# (confirmed against ogsim/common/crypto.py::_decode_key_text). Extract the hex from the CLI's own
# printed public_key_hex= line (never re-derived, never logged as a private value) and write the plain
# form the simulator can actually parse -- flagged in qa/merge-notes.md for the safestop keys.py owner.
HEX=$(grep '^public_key_hex=' /tmp/safestop_keygen.out | cut -d= -f2)
echo "$HEX" > /etc/opengrid/safestop_ed25519.pub
rm -f /tmp/safestop_raw.pub /tmp/safestop_keygen.out

chown opengrid:opengrid /etc/opengrid/guardian_ed25519.key /etc/opengrid/guardian_ed25519.pub \
  /etc/opengrid/safestop_ed25519.key /etc/opengrid/safestop_ed25519.pub
chmod 600 /etc/opengrid/guardian_ed25519.key /etc/opengrid/safestop_ed25519.key
chmod 644 /etc/opengrid/guardian_ed25519.pub /etc/opengrid/safestop_ed25519.pub

echo "== key files =="
ls -la /etc/opengrid/guardian_ed25519.* /etc/opengrid/safestop_ed25519.*
