#!/bin/bash
# r3.4.3 UTILITY-API: install og-sim-utility (ogsim.utility_aen, the simulated Austin Energy EMS).
# For the release manager: run as root on base AFTER the release is deployed to /opt/opengrid/current and
# AFTER r341_utility_api_apache.sh has created /etc/opengrid/utility_sim.env. DRY RUN by default; APPLY=1
# applies. Idempotent. Never prints a secret. Steps:
#   1. MQTT user og_sim_utility: password generated here, hashed into /etc/mosquitto/opengrid.passwd, the
#      clear value appended to /etc/opengrid/utility_sim.env (0640 root:opengrid) as OG_MQTT_UTILITY_PASSWORD;
#   2. ACL: og_sim_utility may only READ og/v1/scenario/cmd (the /ogsim/ triggers); it publishes nothing;
#   3. backups of passwd/acl, then a Mosquitto reload (SIGHUP via systemctl reload), restore on failure;
#   4. install deploy/systemd/og-sim-utility.service and ogsim.target, daemon-reload, enable + start.
set -euo pipefail

APPLY=${APPLY:-0}
MQ_USER=og_sim_utility
PASSWD=/etc/mosquitto/opengrid.passwd
ACL=/etc/mosquitto/opengrid.acl
SIM_ENV=/etc/opengrid/utility_sim.env
SRC=${SRC:-/opt/opengrid/current/deploy/systemd}
TS=$(date +%Y%m%dT%H%M%S)

step() { printf '%s %s\n' "$([ "$APPLY" = 1 ] && echo APPLY || echo DRY)" "$*"; }

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 2; }
for f in "$PASSWD" "$ACL" "$SRC/og-sim-utility.service" "$SRC/ogsim.target"; do
    [ -f "$f" ] || { echo "missing $f" >&2; exit 2; }
done
[ -f "$SIM_ENV" ] || { echo "missing $SIM_ENV: run r341_utility_api_apache.sh first" >&2; exit 2; }

step "backup $PASSWD and $ACL (.bak-$TS)"
if [ "$APPLY" = 1 ]; then cp -a "$PASSWD" "$PASSWD.bak-$TS"; cp -a "$ACL" "$ACL.bak-$TS"; fi

if cut -d: -f1 "$PASSWD" | grep -qx "$MQ_USER"; then
    step "MQTT user $MQ_USER exists; password unchanged"
else
    step "create MQTT user $MQ_USER; store OG_MQTT_UTILITY_PASSWORD in $SIM_ENV"
    if [ "$APPLY" = 1 ]; then
        umask 077
        PW=$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | cut -c1-32)
        mosquitto_passwd -b "$PASSWD" "$MQ_USER" "$PW"
        sed -i '/^OG_MQTT_UTILITY_PASSWORD=/d' "$SIM_ENV"
        printf 'OG_MQTT_UTILITY_PASSWORD=%s\n' "$PW" >>"$SIM_ENV"
        chown root:opengrid "$SIM_ENV"; chmod 0640 "$SIM_ENV"
        unset PW
    fi
fi

if grep -qx "user $MQ_USER" "$ACL"; then
    step "ACL block for $MQ_USER exists"
else
    step "append ACL: user $MQ_USER / topic read og/v1/scenario/cmd"
    if [ "$APPLY" = 1 ]; then printf '\nuser %s\ntopic read og/v1/scenario/cmd\n' "$MQ_USER" >>"$ACL"; fi
fi

step "systemctl reload mosquitto (restore backups if it is not active afterwards)"
if [ "$APPLY" = 1 ]; then
    systemctl reload mosquitto
    sleep 2
    if ! systemctl is-active --quiet mosquitto; then
        cp -a "$PASSWD.bak-$TS" "$PASSWD"; cp -a "$ACL.bak-$TS" "$ACL"; systemctl restart mosquitto
        echo "mosquitto not active after reload; backups restored" >&2; exit 1
    fi
fi

step "install og-sim-utility.service + ogsim.target into /etc/systemd/system; enable --now og-sim-utility"
if [ "$APPLY" = 1 ]; then
    install -m 0644 "$SRC/og-sim-utility.service" /etc/systemd/system/og-sim-utility.service
    [ -f /etc/systemd/system/ogsim.target ] && cp -a /etc/systemd/system/ogsim.target "/etc/systemd/system/ogsim.target.bak-$TS"
    install -m 0644 "$SRC/ogsim.target" /etc/systemd/system/ogsim.target
    systemctl daemon-reload
    systemctl enable --now og-sim-utility.service
    systemctl --no-pager --lines=0 status og-sim-utility.service | head -3
fi
step "done"
