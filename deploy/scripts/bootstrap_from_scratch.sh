#!/bin/bash
# deploy/scripts/bootstrap_from_scratch.sh -- bring up the complete OpenGrid solution (orchestrator, simulators
# and all their data) on a fresh host or database. It automates deploy/BOOTSTRAP.md; read that page first.
#
#   bash deploy/scripts/bootstrap_from_scratch.sh [options]
#
# Phases (default: all, in this order; select with --phase, e.g. --phase c-e or --phase g,h,i):
#   a  prerequisites check (packages, venvs, release tree, owner-supplied files)      read-only
#   b  OS user and directories                                                        BOOTSTRAP.md 1, 4
#   c  create_schema.sh --no-migrate: role (generated password in <etc>/secrets.env), database,
#      extensions, schema og                                                          BOOTSTRAP.md 3
#   d  create_schema.sh: migrations 0001 -> latest (opengrid.platform.db migrate)     BOOTSTRAP.md 6
#   e  seeds, in order: fleet (base + enabled zone blocks) -> market model (utilities, $102 toll, substation
#      asset) -> customer services -> services -> trucks (when the release has them) -> topology; the
#      charge-window default and the firmware catalogue come with 0038 and [firmware.catalogue]; then
#      deploy/scripts/bootstrap_check.py asserts the counts                          BOOTSTRAP.md 7, 8
#   f  sim configs: <etc>/sim/{fleet,scada}.yaml with the approved zone blocks on     BOOTSTRAP.md 8
#   g  Mosquitto users (generated passwords into the env files) and ACL               BOOTSTRAP.md 4, 5
#   h  guardian, safestop and trace-anchor Ed25519 keys                               BOOTSTRAP.md 5
#   i  API proxy secret, Apache conf and UI/customer accounts (install.sh)            BOOTSTRAP.md 4, 5
#   j  systemd units, sim drop-ins, targets enabled, og-lifecycle.timer left disabled BOOTSTRAP.md 6, 9
#   k  optional ERCOT price backfill (only when <etc>/api_keys.env holds the ERCOT keys) BOOTSTRAP.md 10
#   l  first release via deploy.sh (or start), then post-start verification           BOOTSTRAP.md 6, 10
#   x  only deploy/scripts/bootstrap_check.py against the target database (not in the default a-l)
#
# Options:
#   --phase LIST        letters and ranges (a-l, c-e, g,h); default a-l
#   --dry-run           print what each phase would do; change nothing
#   --release DIR       release/repo root (default: this script's repo)
#   --etc DIR           secrets/config dir (default /etc/opengrid)
#   --db-port N --db-name NAME --db-role ROLE   (default 5432 / og / opengrid)
#   --db-host HOST      database host of the DB phases (default 127.0.0.1)
#   --config FILE       orchestrator.toml of the DB phases (default: the release's; its [postgres].host must be
#                       --db-host -- deploy/k8s renders one per pod)
#   --fresh-db          drop the database first (refused on port 5432)
#   --zones LIST        zone blocks to enable (default LZ_AEN, the owner-approved production set)
#   --d32               also enable LZ_LCRA and LZ_RAYBN (decision D-32)
#   --demo-customers    phase l: run dev/scripts/seed_demo_customers.py once the API is up
#   --backfill-days N   phase k window (default 14)
#   --public-url URL    Apache-fronted URL the customer simulator calls (default https://<fqdn>)
#
# Idempotent: every phase converges (existing secrets, keys, users and rows are kept, never rotated or printed).
# Secrets are generated on this host with openssl, written only to <etc> (640 root:opengrid, keys 600 opengrid)
# and /root/opengrid-ui-credentials.txt (600), and never echoed. Runs throttled (idle I/O, nice 19).
set -euo pipefail

if [ -z "${OG_BOOT_THROTTLED:-}" ]; then
  export OG_BOOT_THROTTLED=1
  exec ionice -c3 nice -n 19 env TMPDIR=/dev/shm bash "$0" "$@"
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RELEASE="$(cd "$SCRIPT_DIR/../.." && pwd)"
ETC=/etc/opengrid
DB_HOST=127.0.0.1
CONFIG=""
DB_PORT=5432
DB_NAME=og
DB_ROLE=opengrid
PHASES="a-l"
DRY=0
FRESH_DB=0
ZONES="LZ_AEN"
DEMO=0
BACKFILL_DAYS=14
PUBLIC_URL="https://$(hostname -f 2>/dev/null || hostname)"
OG_PY=/opt/opengrid/venv/bin/python
SIM_PY=/opt/ogsim/venv/bin/python
MQ_DIR=/etc/mosquitto
CREDS=/root/opengrid-ui-credentials.txt
MQTT_ROLES="ENGINE GUARDIAN SIM API SAFESTOP SIMCTL"
SIM_UNITS="og-sim-market og-sim-fleet og-sim-scada og-sim-control"
OG_UNITS="og-feeds og-engine og-guardian og-safestop og-settle og-api"

while [ $# -gt 0 ]; do
  case "$1" in
    --phase) PHASES="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --release) RELEASE="$(cd "$2" && pwd)"; shift 2 ;;
    --etc) ETC="$2"; shift 2 ;;
    --db-port) DB_PORT="$2"; shift 2 ;;
    --db-name) DB_NAME="$2"; shift 2 ;;
    --db-role) DB_ROLE="$2"; shift 2 ;;
    --db-host) DB_HOST="$2"; shift 2 ;;
    --config) CONFIG="$2"; shift 2 ;;
    --fresh-db) FRESH_DB=1; shift ;;
    --zones) ZONES="$2"; shift 2 ;;
    --d32) ZONES="$ZONES,LZ_LCRA,LZ_RAYBN"; shift ;;
    --demo-customers) DEMO=1; shift ;;
    --backfill-days) BACKFILL_DAYS="$2"; shift 2 ;;
    --public-url) PUBLIC_URL="$2"; shift 2 ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
SIM_DIR="$ETC/sim"

# --- helpers -----------------------------------------------------------------------------------------------

# log, die, run and the env-file/secret helpers (env_get, env_set, secure_env, gen_secret, env_ensure) are
# shared with create_schema.sh.
# shellcheck source=deploy/scripts/lib_secrets.sh
. "$SCRIPT_DIR/lib_secrets.sh"
phase() { printf '\n== (%s) %s ==\n' "$1" "$2"; }

expand_phases() {  # "a,c-e" -> "a c d e"
  local out="" part s e c
  for part in ${1//,/ }; do
    if [[ "$part" == ?-? ]]; then
      s=$(printf '%d' "'${part:0:1}"); e=$(printf '%d' "'${part:2:1}")
      for ((c = s; c <= e; c++)); do out+=" $(printf "\\$(printf '%03o' "$c")")"; done
    else
      out+=" $part"
    fi
  done
  echo "$out"
}
SELECTED=" $(expand_phases "$PHASES") "
want() { [[ "$SELECTED" == *" $1 "* ]]; }

# Run a command with the orchestrator's DB environment (password from <etc>/secrets.env, exported only
# inside the subshell).
with_db() {
  (
    set +x
    OG_DB_PASSWORD="$(env_get "$ETC/secrets.env" OG_DB_PASSWORD)"
    [ -n "$OG_DB_PASSWORD" ] || die "OG_DB_PASSWORD missing in $ETC/secrets.env (run phase c)"
    export OG_DB_PASSWORD PGPASSWORD="$OG_DB_PASSWORD"
    export OG_CONFIG="${CONFIG:-$RELEASE/orchestrator/config/orchestrator.toml}" PYTHONPATH="$RELEASE/orchestrator/src"
    export OG_DB="$DB_NAME" OG_DB_PORT="$DB_PORT" OG_DB_USER="$DB_ROLE"
    export PGHOST="$DB_HOST" PGPORT="$DB_PORT" PGUSER="$DB_ROLE" PGDATABASE="$DB_NAME"
    export OG_FLEET_SIM_CONFIG="$SIM_DIR/fleet.yaml"
    "$@"
  )
}
psql_file() { with_db psql -X -q -v ON_ERROR_STOP=1 -f "$1"; }

# --- phases ------------------------------------------------------------------------------------------------

phase_a() {
  phase a "prerequisites"
  local missing=0 c
  for c in psql openssl python3 runuser systemctl; do
    command -v "$c" >/dev/null || { echo "  missing command: $c"; missing=1; }
  done
  for c in mosquitto_passwd htpasswd apache2ctl; do
    command -v "$c" >/dev/null || echo "  WARN: $c not found (needed by phases g/i)"
  done
  [ -x "$OG_PY" ] || { echo "  missing orchestrator venv $OG_PY (live-path installs it: BOOTSTRAP.md 2)"; missing=1; }
  [ -x "$SIM_PY" ] || echo "  WARN: simulator venv $SIM_PY missing (phase l needs it)"
  for d in orchestrator/migrations orchestrator/src integration-sims/config deploy/systemd dev/seed; do
    [ -d "$RELEASE/$d" ] || { echo "  release lacks $d"; missing=1; }
  done
  [ "$(id -u)" -eq 0 ] || { echo "  must run as root"; missing=1; }
  echo "  release: $RELEASE ($(git -C "$RELEASE" describe --tags --always 2>/dev/null || echo 'no git'))"
  echo "  migrations in release: $(ls "$RELEASE"/orchestrator/migrations/[0-9]*.sql | wc -l) (latest $(basename "$(ls "$RELEASE"/orchestrator/migrations/[0-9]*.sql | tail -1)"))"
  echo "  target: db $DB_NAME on port $DB_PORT as $DB_ROLE; secrets in $ETC; zone blocks: $ZONES"
  # Owner-supplied inputs: never generated here.
  if [ -n "$(env_get "$ETC/api_keys.env" ERCOT_PUBLIC_API_KEY_PRIMARY)" ]; then
    echo "  owner input api_keys.env (ERCOT/EIA): present"
  else
    echo "  WARN owner input missing: $ETC/api_keys.env with ERCOT_*/EIA_API_KEY (live feeds and phase k need it;"
    echo "       template docs/orchestrator/07-delivery/integrations/api_keys.env.example)"
  fi
  if [ -n "$(env_get "$ETC/ai_agent.env" ANTHROPIC_API_KEY)" ]; then
    echo "  owner input ai_agent.env (Anthropic): present"
  else
    echo "  WARN owner input missing: $ETC/ai_agent.env ANTHROPIC_API_KEY (optional; the copilot runs its no-model tier)"
  fi
  [ "$missing" -eq 0 ] || die "prerequisites missing"
}

phase_b() {
  phase b "OS user and directories"
  if id opengrid >/dev/null 2>&1; then echo "  user opengrid: present"; else
    run useradd --system --home-dir /opt/opengrid --shell /usr/sbin/nologin opengrid; fi
  run install -d -o root -g opengrid -m 750 "$ETC" "$SIM_DIR"
  if [ "$ETC" = /etc/opengrid ]; then
    run install -d -o opengrid -g opengrid -m 755 /opt/opengrid /opt/opengrid/releases
    run install -d -o opengrid -g opengrid -m 750 /var/lib/opengrid /var/lib/opengrid/anchors \
      /var/lib/opengrid/backups /var/lib/opengrid/ercot_backfill /var/log/opengrid
    run install -d -o opengrid -g opengrid -m 750 /srv/ogbackup/anchors /srv/ogbackup/cold
  fi
}

# Phases c and d are deploy/scripts/create_schema.sh (role, database, extensions, schema; then the migrations).
schema_args() {
  local a=(--release "$RELEASE" --etc "$ETC" --db-port "$DB_PORT" --db-name "$DB_NAME" --db-role "$DB_ROLE"
    --db-host "$DB_HOST")
  [ -z "$CONFIG" ] || a+=(--config "$CONFIG")
  [ "$DRY" -eq 1 ] && a+=(--dry-run)
  printf '%s\n' "${a[@]}"
}
phase_c() {
  phase c "Postgres role, database, extensions and schema (port $DB_PORT)"
  local fresh=(); [ "$FRESH_DB" -eq 1 ] && fresh=(--fresh-db)
  mapfile -t args < <(schema_args)
  OG_PY="$OG_PY" bash "$SCRIPT_DIR/create_schema.sh" "${args[@]}" "${fresh[@]}" --no-migrate
}

phase_d() {
  phase d "migrations"
  mapfile -t args < <(schema_args)
  if [ "$FRESH_DB" -eq 1 ] && [ -f "$RELEASE/orchestrator/schema/og_schema.sql" ]; then
    # A fresh database must match the committed snapshot exactly (make schema-check); an older live database
    # carries lifecycle partitions, so the comparison is made only on a fresh one.
    local rc=0
    OG_PY="$OG_PY" bash "$SCRIPT_DIR/create_schema.sh" "${args[@]}" --check-snapshot || rc=$?
    [ "$rc" -eq 3 ] && echo "  WARN: the fresh schema differs from orchestrator/schema/og_schema.sql (above); seeding continues"
    [ "$rc" -eq 0 ] || [ "$rc" -eq 3 ] || die "create_schema.sh failed ($rc)"
  else
    OG_PY="$OG_PY" bash "$SCRIPT_DIR/create_schema.sh" "${args[@]}"
  fi
}

phase_f() {
  phase f "sim configs ($SIM_DIR, zone blocks $ZONES)"
  local dry=""; [ "$DRY" -eq 1 ] && dry="--dry-run"
  [ "$DRY" -eq 1 ] || install -d -o root -g opengrid -m 750 "$SIM_DIR"
  "$OG_PY" "$SCRIPT_DIR/gen_sim_overrides.py" --release "$RELEASE" --out "$SIM_DIR" --zones "$ZONES" $dry | sed 's/^/  /'
  if [ "$DRY" -eq 0 ]; then chown root:opengrid "$SIM_DIR"/*.yaml; chmod 640 "$SIM_DIR"/*.yaml; fi
}

phase_e() {
  phase e "seeds"
  if [ ! -f "$SIM_DIR/fleet.yaml" ]; then echo "  sim configs missing: running phase f first"; phase_f; fi
  local seed="$RELEASE/dev/seed" trucks=""
  if [ "$DRY" -eq 1 ]; then
    echo "  DRY: fleet seed (OG_FLEET_SIM_CONFIG=$SIM_DIR/fleet.yaml), then market_model_seed.sql,"
    echo "       customer_services_seed.sql, services_seed.sql, mobile_trucks_seed.sql (if present), topology_seed.py"
    return 0
  fi
  log "  1/6 fleet: base 2,000 hubs + enabled zone blocks ($ZONES)"
  with_db "$OG_PY" -m opengrid.fleet.seed 2>&1 | { grep -v '^{' || true; } | sed 's/^/      /'
  log "  2/6 market model: utilities, Austin toll contract (\$102/kW-yr), substation asset sub-LZ_AEN-00"
  psql_file "$seed/market_model_seed.sql"
  log "  3/6 customer services (DATA_CENTER, PIPELINE_AC)"
  psql_file "$seed/customer_services_seed.sql"
  log "  4/6 services (PJM_CAPACITY, MOBILE_STORAGE, LARGE_LOAD)"
  psql_file "$seed/services_seed.sql"
  if [ -f "$seed/mobile_trucks_seed.sql" ]; then
    log "  5/6 mobile trucks"; psql_file "$seed/mobile_trucks_seed.sql"; trucks="--expect-trucks"
  else
    log "  5/6 mobile trucks: SKIPPED (dev/seed/mobile_trucks_seed.sql not in this release)"
  fi
  # Last: it maps every hub already in og.hub, the substation set's and the trucks' included.
  log "  6/6 topology (transformers for every hub, feeder/substation limits, home-bank assets)"
  with_db bash -c '"$0" "$1/dev/seed/topology_seed.py" --fleet-config "$2/fleet.yaml" --scada-config "$2/scada.yaml" \
    --dsn "host=$PGHOST port=$PGPORT dbname=$PGDATABASE user=$PGUSER"' "$OG_PY" "$RELEASE" "$SIM_DIR" | sed 's/^/      /'
  echo "  charge-window default: migration 0038; firmware catalogue: [firmware.catalogue] in orchestrator.toml"
  phase_check "$trucks"
}

phase_check() {
  phase check "seeded database"
  [ "$DRY" -eq 1 ] && { echo "  DRY: $SCRIPT_DIR/bootstrap_check.py $*"; return 0; }
  # shellcheck disable=SC2068
  with_db "$OG_PY" "$SCRIPT_DIR/bootstrap_check.py" $@
}

phase_g() {
  phase g "Mosquitto users and ACL"
  local f="$ETC/secrets.env" role user rc changed=0 regen="" tmp
  for role in $MQTT_ROLES; do
    rc=0; env_ensure "$f" "OG_MQTT_${role}_PASSWORD" || rc=$?
    [ "$rc" -eq 10 ] && regen+=" og_${role,,}"
  done
  rc=0; env_ensure "$ETC/customer_sim.env" OG_MQTT_CUSTOMER_PASSWORD || rc=$?
  [ "$rc" -eq 10 ] && regen+=" og_sim_customer"
  [ "$DRY" -eq 1 ] && { echo "  DRY: ACL $MQ_DIR/opengrid.acl, passwd $MQ_DIR/opengrid.passwd, conf.d/opengrid.conf"; return 0; }
  secure_env "$f"; secure_env "$ETC/customer_sim.env"

  if [ ! -f "$MQ_DIR/opengrid.acl" ]; then
    tmp="$(mktemp)"
    # the six core users of gen_mosquitto_acl.py plus production's two extra grants (shared with deploy/k8s)
    OG_PY="$OG_PY" bash "$SCRIPT_DIR/render_mosquitto_acl.sh" "$tmp" "$f"
    install -o root -g mosquitto -m 640 "$tmp" "$MQ_DIR/opengrid.acl"; rm -f "$tmp"
    echo "  ACL: written ($MQ_DIR/opengrid.acl)"; changed=1
  else
    for user in og_sim og_simctl og_engine og_guardian og_safestop og_api og_sim_customer; do
      grep -qx "user $user" "$MQ_DIR/opengrid.acl" || echo "  WARN: ACL has no block for $user (existing ACL kept; add it by hand)"
    done
    echo "  ACL: present, kept"
  fi

  touch "$MQ_DIR/opengrid.passwd"
  tmp="$(mktemp)"; chmod 600 "$tmp"
  for role in $MQTT_ROLES CUSTOMER; do
    if [ "$role" = CUSTOMER ]; then user=og_sim_customer; src="$ETC/customer_sim.env"; else user="og_${role,,}"; src="$f"; fi
    if ! grep -q "^$user:" "$MQ_DIR/opengrid.passwd" || [[ " $regen " == *" $user "* ]]; then
      printf '%s:%s\n' "$user" "$(env_get "$src" "OG_MQTT_${role}_PASSWORD")" >> "$tmp"
      sed -i "/^$user:/d" "$MQ_DIR/opengrid.passwd"
      echo "  mqtt user $user: set"; changed=1
    else
      echo "  mqtt user $user: present"
    fi
  done
  if [ -s "$tmp" ]; then mosquitto_passwd -U "$tmp"; cat "$tmp" >> "$MQ_DIR/opengrid.passwd"; fi
  rm -f "$tmp"
  chown mosquitto:root "$MQ_DIR/opengrid.passwd"; chmod 640 "$MQ_DIR/opengrid.passwd"

  if [ ! -f "$MQ_DIR/conf.d/opengrid.conf" ]; then
    printf 'listener 1883 127.0.0.1\nallow_anonymous false\npassword_file %s/opengrid.passwd\nacl_file %s/opengrid.acl\n' \
      "$MQ_DIR" "$MQ_DIR" > "$MQ_DIR/conf.d/opengrid.conf"
    chmod 600 "$MQ_DIR/conf.d/opengrid.conf"; changed=1
    echo "  conf.d/opengrid.conf: written"
  fi
  if [ "$changed" -eq 1 ]; then systemctl restart mosquitto; echo "  mosquitto restarted"; fi
}

phase_h() {
  phase h "Ed25519 keys (guardian, safestop, trace anchor)"
  local tmp
  export PYTHONPATH="$RELEASE/orchestrator/src"
  if [ -f "$ETC/guardian_ed25519.key" ]; then echo "  guardian key: present"; else
    run "$OG_PY" -m opengrid.guardian keygen --out "$ETC" --key-id guardian_ed25519 >/dev/null
    echo "  guardian key: generated"; fi
  if [ -f "$ETC/safestop_ed25519.key" ]; then echo "  safestop key: present"; else
    run "$OG_PY" -m opengrid.safestop.keys keygen --key-id safestop_ed25519 \
      --key-out "$ETC/safestop_ed25519.key" --pubkey-out "$ETC/safestop_ed25519.pub" >/dev/null
    echo "  safestop key: generated"; fi
  if [ -f "$ETC/trace_anchor_ed25519.key" ]; then echo "  trace anchor key: present"; else
    if [ "$DRY" -eq 0 ]; then
      tmp="$(mktemp -d)"   # same raw 32-byte seed format; the guardian keygen is the one Ed25519 generator
      "$OG_PY" -m opengrid.guardian keygen --out "$tmp" --key-id trace_anchor_ed25519 >/dev/null
      mv "$tmp/trace_anchor_ed25519.key" "$tmp/trace_anchor_ed25519.pub" "$ETC/"; rm -rf "$tmp"
    fi
    echo "  trace anchor key: generated"; fi
  if [ "$DRY" -eq 0 ]; then
    chown opengrid:opengrid "$ETC"/*_ed25519.key "$ETC"/*_ed25519.pub
    chmod 600 "$ETC"/*_ed25519.key; chmod 644 "$ETC"/*_ed25519.pub
  fi
}

phase_i() {
  phase i "API proxy secret, Apache and UI/customer accounts"
  local secret_conf=/etc/apache2/conf-available/opengrid-proxy-secret.conf user group rc
  env_ensure "$ETC/api_proxy.env" OG_API_PROXY_SECRET root:root 600 || true
  if [ "$DRY" -eq 1 ]; then
    echo "  DRY: $secret_conf (Define), install.sh $RELEASE/deploy (Apache conf, htpasswd operator/viewer/tester/"
    echo "       og-op-a/og-op-b, cron, logrotate, units), og-cust-* accounts + $ETC/customer_sim.env"
    return 0
  fi
  secure_env "$ETC/api_proxy.env" root:root 600
  if [ ! -f "$secret_conf" ] || ! grep -qF "$(env_get "$ETC/api_proxy.env" OG_API_PROXY_SECRET)" "$secret_conf"; then
    ( umask 077; printf 'Define OG_API_PROXY_SECRET %s\n' "$(env_get "$ETC/api_proxy.env" OG_API_PROXY_SECRET)" > "$secret_conf" )
    a2enconf opengrid-proxy-secret >/dev/null
    echo "  Apache proxy secret Define: written"
  else
    echo "  Apache proxy secret Define: present"
  fi
  bash "$RELEASE/deploy/scripts/install.sh" "$RELEASE/deploy" | sed 's/^/  /'
  # Customer accounts from [api.roles.customer]; the customer simulator logs in with the same passwords.
  for user in $(PYTHONPATH='' "$OG_PY" -c 'import sys, tomllib
print(" ".join(tomllib.load(open(sys.argv[1], "rb")).get("api", {}).get("roles", {}).get("customer", {})))' \
      "$RELEASE/orchestrator/config/orchestrator.toml"); do
    group="${user#og-cust-}"; group="${group^^}"
    if grep -q "^$user:" "$ETC/htpasswd" && [ -n "$(env_get "$ETC/customer_sim.env" "OGSIM_CUSTOMER_${group}_PASSWORD")" ]; then
      echo "  $user: present"; continue
    fi
    touch "$ETC/customer_sim.env"; secure_env "$ETC/customer_sim.env"
    env_set "$ETC/customer_sim.env" "OGSIM_CUSTOMER_${group}_USER" "$user"
    env_set "$ETC/customer_sim.env" "OGSIM_CUSTOMER_${group}_PASSWORD" "$(gen_secret)"
    env_get "$ETC/customer_sim.env" "OGSIM_CUSTOMER_${group}_PASSWORD" | htpasswd -iB "$ETC/htpasswd" "$user" 2>/dev/null
    { printf '%s: ' "$user"; env_get "$ETC/customer_sim.env" "OGSIM_CUSTOMER_${group}_PASSWORD"; } >> "$CREDS"
    chmod 600 "$CREDS"
    echo "  $user: created (password in $CREDS and $ETC/customer_sim.env, not displayed)"
  done
  [ -n "$(env_get "$ETC/customer_sim.env" OGSIM_CUSTOMER_API_BASE)" ] || env_set "$ETC/customer_sim.env" OGSIM_CUSTOMER_API_BASE "$PUBLIC_URL"
  secure_env "$ETC/customer_sim.env"; chown root:www-data "$ETC/htpasswd"; chmod 640 "$ETC/htpasswd"
  apache2ctl configtest >/dev/null 2>&1 && systemctl reload apache2 && echo "  apache2 reloaded"
}

phase_j() {
  phase j "systemd units"
  local u
  if [ "$DRY" -eq 1 ]; then
    echo "  DRY: install $RELEASE/deploy/systemd/*.{service,target,timer}; drop-ins og-sim-{fleet,scada}.service.d/austin.conf"
    echo "       -> $SIM_DIR; enable opengrid.target ogsim.target; og-lifecycle.timer stays disabled"
    return 0
  fi
  install -o root -g root -m 644 "$RELEASE"/deploy/systemd/*.service "$RELEASE"/deploy/systemd/*.target /etc/systemd/system/
  if compgen -G "$RELEASE/deploy/systemd/*.timer" >/dev/null; then
    install -o root -g root -m 644 "$RELEASE"/deploy/systemd/*.timer /etc/systemd/system/
  fi
  for u in fleet scada; do  # same drop-in name as production's Austin override
    install -d /etc/systemd/system/og-sim-$u.service.d
    printf '[Service]\nEnvironment=OGSIM_%s_CONFIG=%s/%s.yaml\n' "${u^^}" "$SIM_DIR" "$u" \
      > /etc/systemd/system/og-sim-$u.service.d/austin.conf
  done
  systemctl daemon-reload
  systemctl enable opengrid.target ogsim.target >/dev/null 2>&1
  echo "  units installed; opengrid.target ogsim.target enabled"
  echo "  og-lifecycle.timer: $(systemctl is-enabled og-lifecycle.timer 2>/dev/null || true) (enable after the I/O check, BOOTSTRAP.md 9)"
}

phase_k() {
  phase k "ERCOT price backfill (optional)"
  if [ -z "$(env_get "$ETC/api_keys.env" ERCOT_PUBLIC_API_KEY_PRIMARY)" ]; then
    echo "  SKIPPED: $ETC/api_keys.env has no ERCOT keys (owner-supplied). Forecasts use the pooled rule until"
    echo "  ~14 days of live history accrue; re-run --phase k once the keys are installed."
    return 0
  fi
  if [ "$DRY" -eq 1 ]; then echo "  DRY: tools/ercot_backfill.py --days $BACKFILL_DAYS (<= 6 requests/min)"; return 0; fi
  install -d -o opengrid -g opengrid -m 750 /var/lib/opengrid/ercot_backfill
  with_db bash -c 'set -a; . "$0/api_keys.env"; set +a; exec "$1" "$2/orchestrator/tools/ercot_backfill.py" --days "$3"' \
    "$ETC" "$OG_PY" "$RELEASE" "$BACKFILL_DAYS" 2>&1 | tail -5 | sed 's/^/  /'
}

phase_l() {
  phase l "start and verify"
  local u code=000
  if [ "$DRY" -eq 1 ]; then
    echo "  DRY: $( [ -L /opt/opengrid/current ] && echo "systemctl start opengrid.target ogsim.target" || echo "deploy.sh $RELEASE (first release)")"
    echo "       then: 10 units active, /og/api/health 200, /og/ 401 unauthenticated, bootstrap_check.py --live"
    return 0
  fi
  if [ -L /opt/opengrid/current ]; then
    systemctl start opengrid.target ogsim.target
  else
    install -d -o root -g opengrid -m 750 /opt/opengrid/deploy/scripts
    bash "$RELEASE/deploy/scripts/deploy.sh" "$RELEASE"
  fi
  for _ in $(seq 1 30); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 http://127.0.0.1:8080/og/api/health || true)"
    [ "$code" = 200 ] && break; sleep 2
  done
  echo "  /og/api/health: $code"
  for u in $OG_UNITS $SIM_UNITS; do echo "  $u: $(systemctl is-active "$u" || true)"; done
  echo "  /og/ without credentials: $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/og/ || true) (expect 401)"
  log "  waiting 45 s for the first telemetry and invariant runs"; sleep 45
  phase_check --live || true
  if [ "$DEMO" -eq 1 ]; then
    log "  demo customers (dev/scripts/seed_demo_customers.py)"
    with_db bash -c 'OG_API_PROXY_SECRET="$(sed -n "s/^OG_API_PROXY_SECRET=//p" "$0/api_proxy.env")" exec "$1" "$2/dev/scripts/seed_demo_customers.py"' \
      "$ETC" "$OG_PY" "$RELEASE" | sed 's/^/  /'
  fi
}

# --- main --------------------------------------------------------------------------------------------------

echo "OpenGrid bootstrap from scratch: phases [$(echo $SELECTED)]$( [ "$DRY" -eq 1 ] && echo ' DRY RUN')"
for p in a b c d f e g h i j k l; do
  want "$p" && "phase_$p"
done
want x && phase_check
echo
echo "bootstrap: done"
