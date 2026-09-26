#!/bin/bash
# deploy/scripts/install.sh
#
# One-time (idempotent) root install of everything the 'deploy' role owns on basepower:
# systemd units (installed, NOT enabled/started — app code doesn't exist yet), the Apache
# route + basic auth, the nightly backup cron job, and log rotation. Run as root from a
# directory containing this repo's deploy/ tree (see deploy/README.md).
#
# Never touches: Apache vhosts, MariaDB, mail services, /opt/opengrid_sim, /var/www/html/opengrid,
# /etc/mosquitto (BUILD.md §6 safety rules).
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "install.sh must be run as root" >&2
  exit 1
fi

SRC_DIR="${1:?usage: install.sh <path to the deploy/ directory in this repo>}"
if [ ! -d "$SRC_DIR/systemd" ] || [ ! -d "$SRC_DIR/apache" ]; then
  echo "not a deploy/ directory: $SRC_DIR" >&2
  exit 1
fi

echo "== systemd units (install only, no enable/start) =="
install -o root -g root -m 644 "$SRC_DIR"/systemd/*.service "$SRC_DIR"/systemd/*.target /etc/systemd/system/
systemctl daemon-reload
installed_units="$(cd "$SRC_DIR/systemd" && find . -maxdepth 1 -name '*.service' -o -name '*.target' | sed 's|^\./||' | tr '\n' ' ')"
echo "installed: $installed_units"

echo "== stable deploy/scripts path (/opt/opengrid/deploy/scripts) =="
install -d -o root -g opengrid -m 750 /opt/opengrid/deploy /opt/opengrid/deploy/scripts
install -o root -g opengrid -m 750 "$SRC_DIR"/scripts/deploy.sh "$SRC_DIR"/scripts/rollback.sh "$SRC_DIR"/scripts/backup.sh /opt/opengrid/deploy/scripts/
install -d -o opengrid -g opengrid -m 755 /opt/opengrid/releases
install -d -o opengrid -g opengrid -m 750 /var/lib/opengrid/backups

echo "== grant www-data search access into /etc/opengrid (needed to open htpasswd; secrets.env/api_keys.env stay unreadable to it, they keep mode 640 root:opengrid) =="
if command -v setfacl >/dev/null 2>&1; then
  setfacl -m u:www-data:x /etc/opengrid
else
  echo "setfacl not available, falling back to o+x on /etc/opengrid (traverse only, no listing)" >&2
  chmod o+x /etc/opengrid
fi

echo "== Apache modules =="
for mod in proxy proxy_http headers auth_basic; do
  if ! apache2ctl -M 2>/dev/null | grep -q "^ ${mod}_module"; then
    a2enmod "$mod" >/dev/null
    echo "enabled module: $mod"
  fi
done

echo "== Apache config: /etc/apache2/conf-available/opengrid.conf =="
PREV_CONF=""
if [ -f /etc/apache2/conf-available/opengrid.conf ]; then
  PREV_CONF="$(mktemp)"
  cp /etc/apache2/conf-available/opengrid.conf "$PREV_CONF"
fi
install -o root -g root -m 644 "$SRC_DIR/apache/opengrid.conf" /etc/apache2/conf-available/opengrid.conf
a2enconf opengrid >/dev/null

echo "== htpasswd: operator, viewer, tester =="
HTFILE=/etc/opengrid/htpasswd
CREDS=/root/opengrid-ui-credentials.txt
if [ ! -f "$HTFILE" ]; then
  : > "$HTFILE"
  {
    echo "OpenGrid Orchestrator UI credentials — generated $(date -Is)"
    echo "Do not commit or paste this file anywhere. Regenerate with: htpasswd -B $HTFILE <user>"
    echo
  } > "$CREDS"
  first=1
  for user in operator viewer tester; do
    pass="$(openssl rand -base64 24)"
    if [ "$first" -eq 1 ]; then
      htpasswd -iBc "$HTFILE" "$user" <<<"$pass"
      first=0
    else
      htpasswd -iB "$HTFILE" "$user" <<<"$pass"
    fi
    echo "$user: $pass" >> "$CREDS"
    unset pass
  done
  chmod 600 "$CREDS"
  chown root:root "$CREDS"
  echo "created $HTFILE and $CREDS (not displayed)"
else
  echo "$HTFILE already exists, leaving it and $CREDS untouched"
fi

# K8 two-person stop RELEASE: every operator in [guardian].stop_release_authorised_operators needs an
# Apache account (opengrid.conf requires them), else a fresh install cannot release a safe stop. Missing
# accounts are created with generated passwords appended to $CREDS (0600, never printed); existing ones
# are left untouched. Replace these D-12 test accounts with real per-person accounts (RUNBOOK.md).
echo "== htpasswd: two-person release operators =="
TOML="$SRC_DIR/../orchestrator/config/orchestrator.toml"
RELEASE_USERS="$(python3 -c 'import sys, tomllib
cfg = tomllib.load(open(sys.argv[1], "rb"))
print(" ".join(cfg.get("guardian", {}).get("stop_release_authorised_operators", [])))' "$TOML")"
for user in $RELEASE_USERS; do
  if cut -d: -f1 "$HTFILE" | grep -qx -- "$user"; then
    echo "$user: already present"
    continue
  fi
  pass="$(openssl rand -base64 24)"
  htpasswd -iB "$HTFILE" "$user" <<<"$pass"
  touch "$CREDS"; chmod 600 "$CREDS"; chown root:root "$CREDS"
  echo "$user: $pass" >> "$CREDS"
  unset pass
  echo "$user: created (password in $CREDS, not displayed)"
done
chown root:www-data "$HTFILE"
chmod 640 "$HTFILE"

echo "== configtest before reload =="
if apache2ctl configtest; then
  systemctl reload apache2
  echo "apache2 reloaded"
else
  echo "apache2ctl configtest FAILED — reverting opengrid.conf and NOT reloading" >&2
  if [ -n "$PREV_CONF" ]; then
    cp "$PREV_CONF" /etc/apache2/conf-available/opengrid.conf
  else
    a2disconf opengrid >/dev/null || true
    rm -f /etc/apache2/conf-available/opengrid.conf
  fi
  exit 1
fi
[ -n "$PREV_CONF" ] && rm -f "$PREV_CONF"

echo "== cron: nightly backup =="
install -o root -g root -m 644 "$SRC_DIR/cron/opengrid" /etc/cron.d/opengrid

echo "== logrotate =="
install -o root -g root -m 644 "$SRC_DIR/logrotate/opengrid" /etc/logrotate.d/opengrid

echo "== done =="
echo "systemd units installed but NOT enabled/started (app code not deployed yet)."
echo "UI credentials: $CREDS (mode 600, root only) — never printed here."
