#!/usr/bin/env bash
# deploy/scripts/grid_link_enable_loopback.sh -- enable the D-34 utility grid-control link for AUSTIN_ENERGY on
# LOOPBACK ONLY (127.0.0.1:20001), with locally generated TEST certificates, and optionally run one toll call
# through the Austin Energy simulator's grid_link channel. No firewall change; nothing listens off-host.
#
#   sudo deploy/scripts/grid_link_enable_loopback.sh                 # dry run (default): prints the plan
#   sudo deploy/scripts/grid_link_enable_loopback.sh --apply         # apply + restart og-engine
#   sudo deploy/scripts/grid_link_enable_loopback.sh --apply --test-call   # ... then one call via the AE sim channel
#   sudo deploy/scripts/grid_link_enable_loopback.sh --test-call     # only the test call (link already enabled)
#   sudo deploy/scripts/grid_link_enable_loopback.sh --disable       # dry run of switching the link off
#   sudo deploy/scripts/grid_link_enable_loopback.sh --disable --apply
#
# What --apply does (idempotent; every step says what it did, no secret is ever printed):
#  1. Test PKI under /etc/opengrid/certs (dir 0750 root:opengrid): CA gridlink-test-ca (key 0600 root:root),
#     server og-gridlink (SAN IP:127.0.0.1) and EMS client aen-ems-loopback (keys/certs 0640 root:opengrid).
#     Existing files are kept (--rotate-certs regenerates them).
#  2. OG_MQTT_GRIDLINK_PASSWORD in /etc/opengrid/secrets.env (generated when absent) and MQTT user og_gridlink,
#     publish-only on og/v1/scada/instruction/# (deploy/mosquitto/provision_grid_link_user.py: backs up the
#     broker files, reloads mosquitto, restores on failure).
#  3. /etc/opengrid/grid_link.toml (0640 root:opengrid): [grid_link] enabled, AUSTIN_ENERGY enabled on
#     127.0.0.1:20001, peer 127.0.0.1/32, CN aen-ems-loopback, TLS with the test PKI. LCRA/RAYBURN are not in
#     it, so they stay off. The release config is never edited.
#  4. og-engine drop-in /etc/systemd/system/og-engine.service.d/grid-link.conf setting OG_GRID_LINK_CONFIG to
#     that file; daemon-reload; restart og-engine (unless --no-restart); wait for 127.0.0.1:20001.
# --test-call runs, as user opengrid with the release's ogsim, one 5-minute AUSTIN_ENERGY call of --kw kW
# (default 100) through ogsim.utility_aen.channels.grid_link and cancels it at once unless --keep-call. Outside
# the utility's tolling window the core REFUSES it (e.g. R-CALL-OUTSIDE-WINDOW): that still proves the link, TLS,
# the outstation and the core call function end to end.
#
# Rollback: --disable --apply (removes the drop-in and the override, restarts og-engine: the link is off). The
# certificates, the MQTT user and the secret stay (they grant nothing while the link is off); broker-file
# backups are printed by step 2 if a full revert is wanted (cp -p back, systemctl reload mosquitto).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
RELEASE="${RELEASE:-/opt/opengrid/current}"
CERT_DIR=/etc/opengrid/certs
SECRETS=/etc/opengrid/secrets.env
OVERRIDE=/etc/opengrid/grid_link.toml
DROPIN_DIR=/etc/systemd/system/og-engine.service.d
DROPIN="$DROPIN_DIR/grid-link.conf"
PORT=20001
SERVER_CN=og-gridlink
CLIENT_CN=aen-ems-loopback
CA_CN=gridlink-test-ca
PW_KEY=OG_MQTT_GRIDLINK_PASSWORD
CERT_DAYS=825

DRY=1; TEST_CALL=0; DISABLE=0; RESTART=1; ROTATE=0; KEEP_CALL=0; CALL_KW=100
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) DRY=0 ;;
    --dry-run) DRY=1 ;;
    --test-call) TEST_CALL=1 ;;
    --disable) DISABLE=1 ;;
    --no-restart) RESTART=0 ;;
    --rotate-certs) ROTATE=1 ;;
    --keep-call) KEEP_CALL=1 ;;
    --kw) CALL_KW="${2:?--kw needs a value}"; shift ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

# shellcheck source=lib_secrets.sh
. "$SCRIPT_DIR/lib_secrets.sh"
[ "$(id -u)" -eq 0 ] || die "must run as root"
[[ "$CALL_KW" =~ ^[0-9]+$ ]] && [ "$CALL_KW" -gt 0 ] || die "--kw must be a positive integer"
for cmd in openssl python3 systemctl ss runuser; do command -v "$cmd" >/dev/null || die "$cmd not found"; done
[ -d "$RELEASE/orchestrator" ] || die "release not found: $RELEASE"
[ "$DRY" -eq 1 ] && log "DRY RUN: nothing will change (use --apply)"

topic_root() {
  python3 - "$RELEASE/orchestrator/config/orchestrator.toml" <<'PY'
import sys, tomllib
with open(sys.argv[1], "rb") as fh:
    print(tomllib.load(fh).get("mqtt", {}).get("topic_root", "og/v1"))
PY
}

restart_engine() {
  if [ "$RESTART" -eq 0 ]; then log "og-engine NOT restarted (--no-restart): the change applies at its next restart"; return; fi
  run systemctl daemon-reload
  run systemctl restart og-engine
}

wait_listening() {
  [ "$DRY" -eq 1 ] && { echo "  DRY: wait for 127.0.0.1:$PORT"; return 0; }
  for _ in $(seq 1 30); do
    if ss -Hltn "sport = :$PORT" | grep -q '127.0.0.1'; then log "grid link listening on 127.0.0.1:$PORT (loopback only)"; return 0; fi
    sleep 1
  done
  die "og-engine is not listening on 127.0.0.1:$PORT after 30 s; see: journalctl -u og-engine | grep -i 'grid link'"
}

# -- disable ---------------------------------------------------------------------------------------------------
if [ "$DISABLE" -eq 1 ]; then
  log "disable the grid link on this host"
  [ -f "$DROPIN" ] && run rm -f "$DROPIN" || echo "  drop-in absent"
  [ -f "$OVERRIDE" ] && run rm -f "$OVERRIDE" || echo "  override absent"
  restart_engine
  log "done: og-engine uses the release config's [grid_link] (disabled)"
  exit 0
fi

# -- 1. test PKI -------------------------------------------------------------------------------------------------
make_pki() {
  local tmp ext
  tmp="$(mktemp -d "$CERT_DIR/.gen.XXXXXX")"; chmod 700 "$tmp"
  ext="$tmp/ext.cnf"
  openssl req -x509 -new -nodes -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -days "$CERT_DAYS" \
    -subj "/CN=$CA_CN" -keyout "$tmp/ca.key" -out "$tmp/ca.pem" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -addext "subjectKeyIdentifier=hash" >/dev/null 2>&1
  leaf() {  # name cn eku [san]
    printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyAgreement\nextendedKeyUsage=%s\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n' "$3" > "$ext"
    [ -z "${4:-}" ] || printf 'subjectAltName=%s\n' "$4" >> "$ext"
    openssl req -new -nodes -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -subj "/CN=$2" \
      -keyout "$tmp/$1.key" -out "$tmp/$1.csr" >/dev/null 2>&1
    openssl x509 -req -in "$tmp/$1.csr" -CA "$tmp/ca.pem" -CAkey "$tmp/ca.key" -set_serial "0x$(openssl rand -hex 16)" \
      -days "$CERT_DAYS" -extfile "$ext" -out "$tmp/$1.pem" >/dev/null 2>&1
  }
  leaf "$SERVER_CN" "$SERVER_CN" serverAuth "IP:127.0.0.1"
  leaf "$CLIENT_CN" "$CLIENT_CN" clientAuth
  openssl verify -CAfile "$tmp/ca.pem" "$tmp/$SERVER_CN.pem" "$tmp/$CLIENT_CN.pem" >/dev/null || die "generated certificates do not verify"
  install -m 0600 -o root -g root "$tmp/ca.key" "$CERT_DIR/$CA_CN.key"
  install -m 0640 -o root -g opengrid "$tmp/ca.pem" "$CERT_DIR/$CA_CN.pem"
  for name in "$SERVER_CN" "$CLIENT_CN"; do
    install -m 0640 -o root -g opengrid "$tmp/$name.key" "$CERT_DIR/$name.key"
    install -m 0640 -o root -g opengrid "$tmp/$name.pem" "$CERT_DIR/$name.pem"
  done
  rm -rf "$tmp"
}

log "1. test PKI in $CERT_DIR"
pki_files=("$CA_CN.pem" "$CA_CN.key" "$SERVER_CN.pem" "$SERVER_CN.key" "$CLIENT_CN.pem" "$CLIENT_CN.key")
have=1; for f in "${pki_files[@]}"; do [ -f "$CERT_DIR/$f" ] || have=0; done
if [ "$have" -eq 1 ] && [ "$ROTATE" -eq 0 ]; then
  echo "  present (kept); --rotate-certs regenerates"
elif [ "$DRY" -eq 1 ]; then
  echo "  DRY: generate CA $CA_CN, server $SERVER_CN (SAN IP:127.0.0.1), client $CLIENT_CN (EC P-256, $CERT_DAYS days)"
else
  install -d -m 0750 -o root -g opengrid "$CERT_DIR"
  make_pki
  echo "  generated (keys not displayed): ${pki_files[*]}"
fi

# -- 2. MQTT user ------------------------------------------------------------------------------------------------
log "2. MQTT user og_gridlink (publish-only on $(topic_root)/scada/instruction/#)"
set +e; env_ensure "$SECRETS" "$PW_KEY" root:opengrid 640; set -e
if [ "$DRY" -eq 1 ] && [ -z "$(env_get "$SECRETS" "$PW_KEY")" ]; then
  echo "  DRY: provision og_gridlink with the generated password (hash only in the broker's password file)"
else
  GL_MQTT_PASSWORD="$(env_get "$SECRETS" "$PW_KEY")" \
    python3 "$RELEASE/deploy/mosquitto/provision_grid_link_user.py" \
    "$([ "$DRY" -eq 1 ] && echo --dry-run || echo --apply)" --topic-root "$(topic_root)"
fi

# -- 3. override config ------------------------------------------------------------------------------------------
log "3. $OVERRIDE (AUSTIN_ENERGY on 127.0.0.1:$PORT, peer 127.0.0.1/32, CN $CLIENT_CN)"
override_text() {
  python3 - "$RELEASE/orchestrator/config/orchestrator.toml" <<PY
import sys, tomllib
table = tomllib.load(open(sys.argv[1], "rb"))["grid_link"]
aen = next(u for u in table["utilities"] if u["utility_id"] == "AUSTIN_ENERGY")
banks = ", ".join(f'"{b}"' for b in aen["banks"])
print("# Written by deploy/scripts/grid_link_enable_loopback.sh -- LOOPBACK TEST enablement (D-34).")
print("# Replaces the release [grid_link] via OG_GRID_LINK_CONFIG. Remove with: grid_link_enable_loopback.sh --disable --apply")
print("[grid_link]")
print("enabled = true")
print('mqtt_username = "og_gridlink"')
print('mqtt_password_env = "$PW_KEY"')
print()
print("[[grid_link.utilities]]")
print('utility_id = "AUSTIN_ENERGY"')
print("enabled = true")
print('listen_host = "127.0.0.1"')
print("listen_port = $PORT")
print('allowed_peers = ["127.0.0.1/32"]')
print('allowed_peer_cns = ["$CLIENT_CN"]')
print(f"banks = [{banks}]")
print(f"max_setpoint_kw = {aen['max_setpoint_kw']}")
print(f"heartbeat_timeout_s = {aen.get('heartbeat_timeout_s', 30)}")
for target in aen.get("l2_targets", []):
    print("[[grid_link.utilities.l2_targets]]")
    for key, value in target.items():
        print(f'{key} = "{value}"')
print("[grid_link.utilities.tls]")
print("enabled = true")
print('cert_file = "$CERT_DIR/$SERVER_CN.pem"')
print('key_file = "$CERT_DIR/$SERVER_CN.key"')
print('client_ca_file = "$CERT_DIR/$CA_CN.pem"')
print("[grid_link.utilities.dnp3]")
print(f"outstation_address = {aen.get('dnp3', {}).get('outstation_address', 10)}")
print(f"master_address = {aen.get('dnp3', {}).get('master_address', 1)}")
PY
}
new_override="$(override_text)"
if [ "$DRY" -eq 1 ]; then
  echo "  DRY: write $OVERRIDE (0640 root:opengrid):"; printf '%s\n' "$new_override" | sed 's/^/    /'
else
  tmp="$(mktemp "$OVERRIDE.XXXXXX")"; printf '%s\n' "$new_override" > "$tmp"
  chown root:opengrid "$tmp"; chmod 0640 "$tmp"; mv "$tmp" "$OVERRIDE"
  # validate exactly as og-engine will (fails here, not at engine start)
  runuser -u opengrid -- env OG_GRID_LINK_CONFIG="$OVERRIDE" PYTHONPATH="$RELEASE/orchestrator/src" \
    /opt/opengrid/venv/bin/python -c 'from opengrid.integrations.grid_link.config import grid_link_table, load_grid_link_settings as l
s = l(grid_link_table(None)); a = s.active_utilities()
assert [u.utility_id for u in a] == ["AUSTIN_ENERGY"] and a[0].listen_host == "127.0.0.1", "unexpected override"
print("  override valid: AUSTIN_ENERGY on", a[0].listen_host, a[0].listen_port)'
fi

# -- 4. og-engine drop-in + restart ------------------------------------------------------------------------------
log "4. og-engine drop-in $DROPIN"
dropin_text="[Service]
# D-34 grid link, loopback test enablement (deploy/scripts/grid_link_enable_loopback.sh)
Environment=OG_GRID_LINK_CONFIG=$OVERRIDE"
if [ "$DRY" -eq 1 ]; then
  echo "  DRY: write $DROPIN:"; printf '%s\n' "$dropin_text" | sed 's/^/    /'
else
  install -d -m 0755 "$DROPIN_DIR"; printf '%s\n' "$dropin_text" > "$DROPIN"; chmod 0644 "$DROPIN"
fi
restart_engine
[ "$RESTART" -eq 1 ] && wait_listening

# -- test call ---------------------------------------------------------------------------------------------------
if [ "$TEST_CALL" -eq 1 ]; then
  log "test call: AUSTIN_ENERGY ${CALL_KW} kW for 5 min via ogsim.utility_aen.channels.grid_link"
  if [ "$DRY" -eq 1 ]; then
    echo "  DRY: run one call as user opengrid (TLS as $CLIENT_CN), then cancel it unless --keep-call"
  else
    runuser -u opengrid -- env PYTHONPATH="$RELEASE/integration-sims/src" GL_KW="$CALL_KW" GL_KEEP="$KEEP_CALL" \
      GL_CA="$CERT_DIR/$CA_CN.pem" GL_CERT="$CERT_DIR/$CLIENT_CN.pem" GL_KEY="$CERT_DIR/$CLIENT_CN.key" GL_PORT="$PORT" \
      /opt/ogsim/venv/bin/python - <<'PY'
import asyncio, os, time
from datetime import UTC, datetime
from ogsim.utility_aen.channels import build_channel
from ogsim.utility_aen.channels.base import CallSpec

async def main() -> int:
    channel = build_channel("grid_link", {
        "host": "127.0.0.1", "port": int(os.environ["GL_PORT"]), "status_wait_s": 10, "timeout_s": 5,
        "tls": {"ca_file": os.environ["GL_CA"], "cert_file": os.environ["GL_CERT"],
                "key_file": os.environ["GL_KEY"], "server_hostname": "127.0.0.1"},
    })
    ref = f"loopback-{int(time.time())}"
    try:
        result = await channel.issue_call(CallSpec(ref, -float(os.environ["GL_KW"]), datetime.now(UTC), 5, "grid link loopback test"))
        print(f"  call {ref}: state={result.state} accepted={result.accepted} reason={result.reason_code}")
        if result.accepted and os.environ["GL_KEEP"] != "1":
            ended = await channel.cancel(ref)
            print(f"  cancelled: state={ended.state} reason={ended.reason_code}")
        return 0 if result.state in ("ACCEPTED", "ACTIVE", "REFUSED", "COMPLETED") else 1
    finally:
        await channel.aclose()

raise SystemExit(asyncio.run(main()))
PY
    echo "  a REFUSED with an R-CALL-* reason (e.g. outside the tolling window) proves the link end to end;"
    echo "  audit: stream grid_link:AUSTIN_ENERGY (GRID_LINK_COMMAND) and dispatch_call:grid_link:AUSTIN_ENERGY"
  fi
fi
log "done"
