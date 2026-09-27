#!/bin/bash
# tests-perf/stack.sh -- lifecycle of the ISOLATED performance stack (run as root on the base server).
#
#   bash tests-perf/stack.sh [--run DIR] <command>
#
#   keys         perf-only Ed25519 keys (guardian, safestop, trace anchor) under <run>/keys
#   db           fresh og_perf on the TEST cluster (5433) via bootstrap_from_scratch.sh phases c-e
#   broker-up    start the perf Mosquitto (own port/config/ACL/password file; /etc/mosquitto untouched)
#   broker-down  stop it (exact unit + recorded PID)
#   up           broker-up, then sims and og-* processes
#   down         stop every ogperf-* unit this script started, by exact unit name and recorded MainPID
#   status       unit, MainPID, state, restarts, RSS
#   drop-db      drop og_perf, its tmpfs tablespace and the og_perf role on 5433
#
# Every process is a transient systemd unit `ogperf-<name>` (systemd-run), User=opengrid (Mosquitto:
# mosquitto), Nice=19, CPUWeight=10, IOWeight=10, IOSchedulingClass=idle, so production's units always win
# the CPU. IOWeight/idle class only act under the bfq scheduler (sda ran `none` until 2026-09-26 22:30);
# og_perf's data files live on tmpfs for that reason. Started PIDs are appended to <run>/pids.tsv.
# Nothing is ever stopped by pattern.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN="$SCRIPT_DIR/.run"
OG_PY=/opt/opengrid/venv/bin/python
SIM_PY=/opt/ogsim/venv/bin/python

while [ $# -gt 1 ]; do
  case "$1" in
    --run) RUN="$2"; shift 2 ;;
    *) break ;;
  esac
done
CMD="${1:-status}"

[ "$(id -u)" -eq 0 ] || { echo "stack.sh: run as root" >&2; exit 2; }
[ -f "$RUN/ports.env" ] || { echo "stack.sh: $RUN/ports.env missing -- run perfenv.py first" >&2; exit 2; }
# shellcheck disable=SC1091
. "$RUN/ports.env"
[ "$DB_PORT" != 5432 ] || { echo "refusing: DB_PORT is production" >&2; exit 2; }
[ "$MQTT_PORT" != 1883 ] || { echo "refusing: MQTT_PORT is production" >&2; exit 2; }

TS_NAME=ogperf_ram
TS_DIR=/dev/shm/ogperf_ts
OG_UNITS="engine guardian safestop settle feeds api"
SIM_UNITS="sim-market sim-fleet sim-scada"
ALL_UNITS="mosquitto $SIM_UNITS $OG_UNITS"

THROTTLE=(-p Nice=19 -p CPUWeight=10 -p IOWeight=10 -p IOSchedulingClass=idle)

log() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*"; }

record_pid() {  # unit
  local pid
  pid="$(systemctl show -p MainPID --value "ogperf-$1.service" 2>/dev/null || echo 0)"
  printf '%s\togperf-%s\t%s\n' "$(date -Is)" "$1" "$pid" >> "$RUN/pids.tsv"
  log "  ogperf-$1 MainPID=$pid"
}

unit_active() { systemctl is-active --quiet "ogperf-$1.service"; }

start_unit() {  # name user workdir memmax -- cmd...
  local name="$1" user="$2" wd="$3" mem="$4"; shift 5
  if unit_active "$name"; then log "  ogperf-$name already active"; return 0; fi
  systemctl reset-failed "ogperf-$name.service" >/dev/null 2>&1 || true
  systemd-run --quiet --collect --unit="ogperf-$name" \
    -p User="$user" -p Group="$user" "${THROTTLE[@]}" -p MemoryMax="$mem" \
    -p Restart=on-failure -p RestartSec=2 -p WorkingDirectory="$wd" \
    -p EnvironmentFile="$RUN/etc/secrets.env" \
    "${ENVS[@]}" "$@"
  sleep 0.5
  record_pid "$name"
}

og_env() {
  ENVS=(--setenv=PYTHONPATH="$REPO/orchestrator/src" --setenv=OG_CONFIG="$RUN/config/orchestrator.toml"
        --setenv=OG_DB="$DB_NAME" --setenv=OG_DB_PORT="$DB_PORT" --setenv=OG_DB_USER="$DB_ROLE"
        --setenv=OG_API_BASE_URL="http://127.0.0.1:$API_PORT"
        --setenv=OG_TDSP_TARIFFS_PATH="$RUN/config/tdsp_tariffs.toml"
        --setenv=OG_FLEET_SIM_CONFIG="$RUN/etc/sim/fleet.yaml"
        --setenv=PYTHONDONTWRITEBYTECODE=1)
}

sim_env() {
  ENVS=(--setenv=PYTHONPATH="$REPO/integration-sims/src" --setenv=OG_MQTT_HOST=127.0.0.1
        --setenv=OG_MQTT_PORT="$MQTT_PORT" --setenv=OG_MQTT_ROOT="$TOPIC_ROOT"
        --setenv=OGSIM_FLEET_CONFIG="$RUN/etc/sim/fleet.yaml" --setenv=OGSIM_SCADA_CONFIG="$RUN/etc/sim/scada.yaml"
        --setenv=OGSIM_GUARDIAN_PUBLIC_KEY_PATH="$RUN/keys/guardian_ed25519.pub"
        --setenv=OGSIM_SAFESTOP_PUBLIC_KEY_PATH="$RUN/keys/safestop_ed25519.pub"
        --setenv=OGSIM_CONFIG_DIR="$RUN/etc/sim" --setenv=OGSIM_MARKET_PORT="$MARKET_PORT"
        --setenv=OGSIM_MARKET_HOST=127.0.0.1 --setenv=OGSIM_MARKET_DATA_MODE=synthetic
        --setenv=OGSIM_MARKET_ANOMALY_LOG_PATH="$RUN/var/market_anomalies.jsonl"
        --setenv=PYTHONDONTWRITEBYTECODE=1)
}

perms() {
  chown root:opengrid "$RUN" "$RUN/etc" "$RUN/etc/sim" "$RUN/config" "$RUN/keys"
  chmod 755 "$RUN"; chmod 750 "$RUN/etc" "$RUN/etc/sim" "$RUN/keys"
  chmod -R g+rX "$RUN/config"
  chown root:opengrid "$RUN"/etc/sim/*.yaml; chmod 640 "$RUN"/etc/sim/*.yaml
  chown -R opengrid:opengrid "$RUN/var" "$RUN/data"
  chown root:opengrid "$RUN/etc/secrets.env"; chmod 640 "$RUN/etc/secrets.env"
  chown -R root:mosquitto "$RUN/mosquitto"; chmod 750 "$RUN/mosquitto"; chmod 640 "$RUN"/mosquitto/*
}

cmd_keys() {
  local tmp
  if [ ! -f "$RUN/keys/guardian_ed25519.key" ]; then
    PYTHONPATH="$REPO/orchestrator/src" "$OG_PY" -m opengrid.guardian keygen --out "$RUN/keys" --key-id guardian_ed25519 >/dev/null
  fi
  if [ ! -f "$RUN/keys/safestop_ed25519.key" ]; then
    PYTHONPATH="$REPO/orchestrator/src" "$OG_PY" -m opengrid.safestop.keys keygen --key-id safestop_ed25519 \
      --key-out "$RUN/keys/safestop_ed25519.key" --pubkey-out "$RUN/keys/safestop_ed25519.pub" >/dev/null
  fi
  if [ ! -f "$RUN/keys/trace_anchor_ed25519.key" ]; then
    tmp="$(mktemp -d)"
    PYTHONPATH="$REPO/orchestrator/src" "$OG_PY" -m opengrid.guardian keygen --out "$tmp" --key-id trace_anchor_ed25519 >/dev/null
    mv "$tmp"/trace_anchor_ed25519.* "$RUN/keys/"; rmdir "$tmp"
  fi
  chown opengrid:opengrid "$RUN"/keys/*; chmod 600 "$RUN"/keys/*.key; chmod 644 "$RUN"/keys/*.pub
  log "keys: $(ls "$RUN/keys" | tr '\n' ' ')"
}

pg_admin() { runuser -u postgres -- psql -X -q -v ON_ERROR_STOP=1 -p "$DB_PORT" "$@"; }

cmd_db() {
  # Data files on tmpfs (tablespace ogperf_ram inside the ogtest cluster): base's pgdata and pgstandby share
  # one virtual HDD, so only og_perf's WAL may touch the disk (owner-approved 2026-09-26). The database is
  # created here in that tablespace; bootstrap phases c-e then keep it (no --fresh-db) and migrate + seed.
  log "db: fresh $DB_NAME on port $DB_PORT in tablespace $TS_NAME ($TS_DIR), bootstrap phases c-e"
  pg_admin -c "DROP DATABASE IF EXISTS \"$DB_NAME\" WITH (FORCE)"
  install -d -o postgres -g postgres -m 700 "$TS_DIR"
  if [ "$(pg_admin -Atc "select count(*) from pg_tablespace where spcname = '$TS_NAME'")" = 0 ]; then
    pg_admin -c "CREATE TABLESPACE $TS_NAME LOCATION '$TS_DIR'"
  fi
  if [ "$(pg_admin -Atc "select count(*) from pg_roles where rolname = '$DB_ROLE'")" = 0 ]; then
    pg_admin -c "CREATE ROLE \"$DB_ROLE\" LOGIN"
  fi
  pg_admin -c "CREATE DATABASE \"$DB_NAME\" OWNER \"$DB_ROLE\" TABLESPACE $TS_NAME"
  local rc=0
  bash "$REPO/deploy/scripts/bootstrap_from_scratch.sh" --phase c-e --release "$REPO" \
    --etc "$RUN/etc" --db-port "$DB_PORT" --db-name "$DB_NAME" --db-role "$DB_ROLE" > "$RUN/logs/bootstrap.log" 2>&1 || rc=$?
  grep -E 'seeded|OK|FAIL|ERROR|migrations:' "$RUN/logs/bootstrap.log" | tail -25 || true
  log "db: bootstrap exit $rc (a FAIL in bootstrap_check's fixed production counts is expected off 3,500 homes)"
  local hubs
  hubs="$(runuser -u postgres -- psql -X -p "$DB_PORT" -d "$DB_NAME" -Atc 'select count(*) from og.hub')"
  log "db: og.hub rows = $hubs (expected $EXPECTED_HUBS)"
  [ "$hubs" = "$EXPECTED_HUBS" ] || { echo "seed mismatch" >&2; exit 3; }
  perms
}

cmd_broker_up() {
  perms
  local tmp
  tmp="$(mktemp)"; chmod 600 "$tmp"
  for role in ENGINE GUARDIAN SIM API SAFESTOP SIMCTL PERFMON; do
    printf 'og_%s:%s\n' "${role,,}" "$(sed -n "s/^OG_MQTT_${role}_PASSWORD=//p" "$RUN/etc/secrets.env")" >> "$tmp"
  done
  mosquitto_passwd -U "$tmp"
  install -o root -g mosquitto -m 640 "$tmp" "$RUN/mosquitto/passwd"; rm -f "$tmp"
  ENVS=()
  start_unit mosquitto mosquitto "$RUN/mosquitto" 1G -- /usr/sbin/mosquitto -c "$RUN/mosquitto/mosquitto.conf"
  for _ in $(seq 1 20); do ss -ltn | grep -q "127.0.0.1:$MQTT_PORT " && break; sleep 0.5; done
  ss -ltn | grep -q "127.0.0.1:$MQTT_PORT " || { echo "perf broker did not listen on $MQTT_PORT" >&2; exit 3; }
}

stop_unit() {  # name
  local u="ogperf-$1.service" pid
  pid="$(systemctl show -p MainPID --value "$u" 2>/dev/null || echo 0)"
  if [ -n "$pid" ] && [ "$pid" != 0 ]; then
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 40); do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done
    kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null || true
  fi
  systemctl stop "$u" >/dev/null 2>&1 || true
  systemctl reset-failed "$u" >/dev/null 2>&1 || true
  printf '%s\togperf-%s\tstopped(pid %s)\n' "$(date -Is)" "$1" "${pid:-0}" >> "$RUN/pids.tsv"
  log "  stopped ogperf-$1 (pid ${pid:-0})"
}

cmd_up() {
  cmd_broker_up
  sim_env
  start_unit sim-market opengrid "$REPO/integration-sims" 1G -- "$SIM_PY" -m ogsim.market
  start_unit sim-fleet opengrid "$REPO/integration-sims" 6G -- "$SIM_PY" -m ogsim.fleet
  start_unit sim-scada opengrid "$REPO/integration-sims" 1G -- "$SIM_PY" -m ogsim.scada
  og_env
  start_unit safestop opengrid "$REPO/orchestrator" 512M -- "$OG_PY" -m opengrid.safestop.main
  start_unit guardian opengrid "$REPO/orchestrator" 3G -- "$OG_PY" -m opengrid.guardian.main
  start_unit engine opengrid "$REPO/orchestrator" 4G -- "$OG_PY" -m opengrid.engine.main
  start_unit feeds opengrid "$REPO/orchestrator" 1G -- "$OG_PY" -m opengrid.feeds.main
  start_unit settle opengrid "$REPO/orchestrator" 2G -- "$OG_PY" -m opengrid.settle.main
  start_unit api opengrid "$REPO/orchestrator" 2G -- "$OG_PY" -m opengrid.api.main
}

cmd_down() {
  local u
  for u in $OG_UNITS; do unit_active "$u" && stop_unit "$u"; done
  for u in $SIM_UNITS; do unit_active "$u" && stop_unit "$u"; done
  unit_active mosquitto && stop_unit mosquitto
  # anything left in a failed/restarting state
  for u in $ALL_UNITS; do systemctl reset-failed "ogperf-$u.service" >/dev/null 2>&1 || true; done
  true
}

cmd_status() {
  local u pid state nr rss
  for u in $ALL_UNITS; do
    pid="$(systemctl show -p MainPID --value "ogperf-$u.service" 2>/dev/null || echo 0)"
    state="$(systemctl is-active "ogperf-$u.service" 2>/dev/null || true)"
    nr="$(systemctl show -p NRestarts --value "ogperf-$u.service" 2>/dev/null || echo -)"
    rss=0; [ "${pid:-0}" != 0 ] && rss="$(awk '/VmRSS/{print $2}' "/proc/$pid/status" 2>/dev/null || echo 0)"
    printf '%-20s pid=%-8s %-10s restarts=%-3s rss_kb=%s\n' "ogperf-$u" "${pid:-0}" "$state" "$nr" "$rss"
  done
}

cmd_drop_db() {
  pg_admin -c "DROP DATABASE IF EXISTS \"$DB_NAME\" WITH (FORCE)"
  pg_admin -c "DROP TABLESPACE IF EXISTS $TS_NAME"
  pg_admin -c "DROP ROLE IF EXISTS \"$DB_ROLE\""
  [ -d "$TS_DIR" ] && rmdir "$TS_DIR" 2>/dev/null || true
  log "dropped database $DB_NAME, tablespace $TS_NAME ($TS_DIR) and role $DB_ROLE on port $DB_PORT"
}

case "$CMD" in
  keys) cmd_keys ;;
  db) cmd_db ;;
  broker-up) cmd_broker_up ;;
  broker-down) stop_unit mosquitto ;;
  up) cmd_up ;;
  down) cmd_down ;;
  down-all) for h in sampler watchdog; do unit_active "$h" && stop_unit "$h"; done; cmd_down ;;
  status) cmd_status ;;
  drop-db) cmd_drop_db ;;
  *) echo "unknown command: $CMD" >&2; exit 2 ;;
esac
