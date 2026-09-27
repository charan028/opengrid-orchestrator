#!/bin/bash
# r3.4.1 UTILITY-API (D-33): the Austin Energy Apache account and its access to /og/api/customer/.
# For the release manager: run as root on base. DRY RUN by default (prints the steps, changes nothing);
# APPLY=1 applies. Idempotent. Never prints a secret: the password is generated here and written only to
#   /root/opengrid-utility-credentials.txt   (0600 root)       -- for the owner / Austin Energy hand-over
#   /etc/opengrid/utility_sim.env            (0640 root:opengrid) -- for og-sim-utility (ogsim.utility_aen)
# Steps: backup -> htpasswd account (bcrypt) -> Require line -> apache2ctl configtest -> reload; on a failed
# configtest the backups are restored and nothing is reloaded.
set -euo pipefail

APPLY=${APPLY:-0}
USER_NAME=og-util-aen
CONF=/etc/apache2/conf-available/opengrid.conf
HTPASSWD=/etc/opengrid/htpasswd
CRED=/root/opengrid-utility-credentials.txt
SIM_ENV=/etc/opengrid/utility_sim.env
API_BASE=https://base.tocy-net.net
TS=$(date +%Y%m%dT%H%M%S)

step() { printf '%s %s\n' "$([ "$APPLY" = 1 ] && echo APPLY || echo DRY)" "$*"; }

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 2; }
[ -f "$CONF" ] && [ -f "$HTPASSWD" ] || { echo "missing $CONF or $HTPASSWD" >&2; exit 2; }

step "backup $CONF -> $CONF.bak-$TS and $HTPASSWD -> $HTPASSWD.bak-$TS"
if [ "$APPLY" = 1 ]; then cp -a "$CONF" "$CONF.bak-$TS"; cp -a "$HTPASSWD" "$HTPASSWD.bak-$TS"; fi

if cut -d: -f1 "$HTPASSWD" | grep -qx "$USER_NAME"; then
    step "account $USER_NAME already exists; password unchanged"
else
    step "create $USER_NAME in $HTPASSWD (bcrypt), write $CRED (0600) and $SIM_ENV (0640 root:opengrid)"
    if [ "$APPLY" = 1 ]; then
        umask 077
        PW=$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | cut -c1-32)
        printf '%s\n' "$PW" | htpasswd -i -B "$HTPASSWD" "$USER_NAME" >/dev/null 2>&1
        chown root:www-data "$HTPASSWD"; chmod 0640 "$HTPASSWD"
        printf 'user=%s\npassword=%s\nurl=%s/og/api/customer/v1/utility/\n' "$USER_NAME" "$PW" "$API_BASE" >"$CRED"
        chmod 0600 "$CRED"
        printf 'OGSIM_UTILITY_API_BASE=%s\nOGSIM_UTILITY_AEN_USER=%s\nOGSIM_UTILITY_AEN_PASSWORD=%s\n' \
            "$API_BASE" "$USER_NAME" "$PW" >"$SIM_ENV"
        chown root:opengrid "$SIM_ENV"; chmod 0640 "$SIM_ENV"
        unset PW
    fi
fi

if grep -Eq "^\s*Require user og-cust-.*\b$USER_NAME\b" "$CONF"; then
    step "Require line under /og/api/customer/ already lists $USER_NAME"
else
    step "append $USER_NAME to the Require line of <Location /og/api/customer/>"
    if [ "$APPLY" = 1 ]; then
        sed -i -E "/<Location \/og\/api\/customer\/>/,/<\/Location>/ s/^(\s*Require user og-cust-.*)$/\1 $USER_NAME/" "$CONF"
        grep -Eq "^\s*Require user og-cust-.*\b$USER_NAME\b" "$CONF" || { echo "Require edit failed" >&2; exit 1; }
    fi
fi

step "apache2ctl configtest && systemctl reload apache2 (restore backups on failure)"
if [ "$APPLY" = 1 ]; then
    if apache2ctl configtest >/dev/null 2>&1; then
        systemctl reload apache2
    else
        cp -a "$CONF.bak-$TS" "$CONF"; cp -a "$HTPASSWD.bak-$TS" "$HTPASSWD"
        echo "configtest failed; backups restored, apache not reloaded" >&2
        exit 1
    fi
fi
step "done"
