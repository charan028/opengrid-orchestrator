#!/bin/bash
# deploy/k8s/images/og-entrypoint.sh -- the single entrypoint of both OpenGrid images (deploy/k8s/README.md).
# The first argument selects what the container runs; each case is the systemd unit's ExecStart
# (deploy/systemd/*.service) or a reused deploy script, never a re-implementation.
#
#   orchestrator image: engine guardian safestop feeds settle api lifecycle
#                       migrate seed check keygen-tar mqtt-acl wait-db wait-schema probe-http probe-heartbeat
#                       demo-customers
#   sims image:         sim-market sim-fleet sim-scada sim-control sim-customer sim-utility sim-configs
#
# Environment (set by the chart): OG_CONFIG (rendered config path), OG_CONFIG_OVERRIDES (TOML fragment),
# OG_DB/OG_DB_USER/OG_DB_PORT/OG_DB_PASSWORD, OG_ZONES (enabled zone blocks), OG_SIM_DIR.
# Nothing here prints a secret.
set -euo pipefail

RELEASE=/opt/opengrid/current
OG_PY=/opt/opengrid/venv/bin/python
SIM_PY=/opt/ogsim/venv/bin/python
SUPPORT=/opt/opengrid/k8s/k8s_support.py
OG_CONFIG_BASE="$RELEASE/orchestrator/config/orchestrator.toml"
export OG_CONFIG="${OG_CONFIG:-/run/og/orchestrator.toml}"
OG_CONFIG_OVERRIDES="${OG_CONFIG_OVERRIDES:-/etc/opengrid-k8s/overrides.toml}"
OG_SIM_DIR="${OG_SIM_DIR:-/run/og/sim}"
OG_ZONES="${OG_ZONES:-LZ_AEN}"

render_config() {
  "$OG_PY" "$SUPPORT" render-config --base "$OG_CONFIG_BASE" --overrides "$OG_CONFIG_OVERRIDES" --out "$OG_CONFIG"
}

# The sim configs with the approved zone blocks on (deploy/scripts/gen_sim_overrides.py; deterministic, so the
# seed Job, the check and the fleet/scada simulators all see the same fleet).
sim_configs() {
  local py="$1"
  "$py" "$RELEASE/deploy/scripts/gen_sim_overrides.py" --release "$RELEASE" --out "$OG_SIM_DIR" --zones "$OG_ZONES"
}

# The deploy scripts' DB options and <etc>/secrets.env (OG_DB_PASSWORD, written from the pod's environment into a
# private tmpfs dir: bootstrap_from_scratch.sh and create_schema.sh read the password from there, never argv).
ETC_DIR=/run/og/etc
db_etc() {
  install -d -m 700 "$ETC_DIR"
  ( umask 077; printf 'OG_DB_PASSWORD=%s\n' "${OG_DB_PASSWORD:?OG_DB_PASSWORD unset}" > "$ETC_DIR/secrets.env" )
  DB_ARGS=(--config "$OG_CONFIG" --db-host "${OG_DB_HOST:?OG_DB_HOST unset}" --db-port "${OG_DB_PORT:-5432}"
    --db-name "${OG_DB:-og}" --db-role "${OG_DB_USER:-opengrid}")
}

wait_server() {  # the server accepts connections (before the role exists, so pg_isready, not a login)
  local i
  for i in $(seq 1 360); do
    pg_isready -q -h "$OG_DB_HOST" -p "${OG_DB_PORT:-5432}" && return 0
    [ $((i % 12)) -ne 1 ] || echo "wait-server: $OG_DB_HOST:${OG_DB_PORT:-5432} not accepting connections yet"
    sleep 5
  done
  echo "wait-server: timed out" >&2
  exit 1
}

og_service() {  # module [args...]
  render_config
  exec "$OG_PY" -m "$@"
}

sim_service() {  # module
  cd "$RELEASE/integration-sims"
  exec "$SIM_PY" -m "$1"
}

svc="${1:-}"
[ -n "$svc" ] || { sed -n '2,14p' "$0"; exit 2; }
shift

case "$svc" in
  engine)    og_service opengrid.engine.main "$@" ;;
  guardian)  og_service opengrid.guardian.main "$@" ;;
  safestop)  og_service opengrid.safestop.main "$@" ;;
  feeds)     og_service opengrid.feeds.main "$@" ;;
  settle)    og_service opengrid.settle.main "$@" ;;
  api)       og_service opengrid.api "$@" ;;
  lifecycle) og_service opengrid.lifecycle run --cycle auto "$@" ;;

  wait-db|wait-schema)
    render_config
    exec "$OG_PY" "$SUPPORT" "$svc" "$@" ;;

  migrate)  # BOOTSTRAP.md 3 and 6: deploy/scripts/create_schema.sh (role, database, schema og, every migration)
    render_config
    db_etc
    wait_server
    if [ -n "${OG_PG_ADMIN_PASSWORD:-}" ]; then
      exec bash "$RELEASE/deploy/scripts/create_schema.sh" --etc "$ETC_DIR" "${DB_ARGS[@]}"
    fi
    # External server without admin credentials: the role and database exist (README "External database"); the
    # migration runner creates schema og itself.
    exec "$OG_PY" -m opengrid.platform.db migrate ;;

  seed)  # bootstrap phase e (all seeds in order, then bootstrap_check.py), against the Service DB
    render_config
    db_etc
    "$OG_PY" "$SUPPORT" wait-schema
    exec bash "$RELEASE/deploy/scripts/bootstrap_from_scratch.sh" --phase e --etc "$ETC_DIR" "${DB_ARGS[@]}" \
      --zones "$OG_ZONES" ;;

  check)  # deploy/scripts/bootstrap_check.py [--live]; trucks expected when the release ships their seed (phase e)
    render_config
    sim_configs "$OG_PY" >/dev/null
    export OG_FLEET_SIM_CONFIG="$OG_SIM_DIR/fleet.yaml"
    trucks=()
    [ ! -f "$RELEASE/dev/seed/mobile_trucks_seed.sql" ] || trucks=(--expect-trucks)
    exec "$OG_PY" "$RELEASE/deploy/scripts/bootstrap_check.py" "${trucks[@]}" "$@" ;;

  keygen-tar)  # bootstrap phase h into a private tmpfs dir, streamed as a tar on stdout (install.sh reads it)
    out="$(mktemp -d)"
    bash "$RELEASE/deploy/scripts/bootstrap_from_scratch.sh" --phase h --etc "$out" >&2
    tar -C "$out" -cf - guardian_ed25519.key guardian_ed25519.pub safestop_ed25519.key safestop_ed25519.pub \
      trace_anchor_ed25519.key trace_anchor_ed25519.pub
    rm -rf "$out" ;;

  mqtt-acl)  # the production ACL (deploy/scripts/render_mosquitto_acl.sh) into $1; with OG_GRIDLINK_ACL=1 also
             # the og_gridlink block of deploy/mosquitto/provision_grid_link_user.py (its render_acl)
    bash "$RELEASE/deploy/scripts/render_mosquitto_acl.sh" "${1:?output path}"
    if [ "${OG_GRIDLINK_ACL:-0}" = 1 ]; then
      "$OG_PY" - "$1" "$RELEASE/deploy/mosquitto" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[2])
from provision_grid_link_user import render_acl  # noqa: E402

acl = Path(sys.argv[1])
acl.write_text(render_acl(acl.read_text(encoding="utf-8"), "og/v1"), encoding="utf-8")
PY
    fi ;;

  probe-http|probe-heartbeat)
    exec "$OG_PY" "$SUPPORT" "$svc" "$@" ;;

  demo-customers)  # dev/scripts/seed_demo_customers.py against the loopback API (run inside the og-api pod)
    exec "$OG_PY" "$SUPPORT" exec-demo-customers "$@" ;;

  sim-configs) sim_configs "$SIM_PY" ;;
  sim-market)   sim_service ogsim.market ;;
  sim-fleet)    sim_service ogsim.fleet ;;
  sim-scada)    sim_service ogsim.scada ;;
  sim-control)  sim_service ogsim.control ;;
  sim-customer) sim_service ogsim.customer ;;
  sim-utility)  sim_service ogsim.utility_aen ;;

  *) echo "og-entrypoint: unknown service '$svc'" >&2; exit 2 ;;
esac
