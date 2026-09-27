#!/bin/sh
# dev/scripts/gen-mosquitto-passwd.sh -- runs INSIDE the `mosquitto-init` one-shot container
# (image eclipse-mosquitto:2, which ships the `mosquitto_passwd` binary; there is no portable
# pure-Python equivalent of Mosquitto's PBKDF2 password-file format worth hand-rolling for a
# dev tool). Reads the six OG_MQTT_*_PASSWORD values from the environment (docker-compose.yml
# passes them via `env_file: ../secrets`, generated from dev/secrets.example) and writes a
# freshly hashed password file every `make dev-up`, so rotating a password in dev/secrets and
# re-running dev-up is enough -- no manual mosquitto_passwd invocation needed.
set -eu

OUT=/mosquitto/secrets/passwd
rm -f "$OUT"

require_var() {
    var_name=$1
    eval "value=\${$var_name:-}"
    if [ -z "$value" ]; then
        echo "gen-mosquitto-passwd.sh: $var_name is not set (copy dev/secrets.example to dev/secrets first)" >&2
        exit 1
    fi
}

for v in OG_MQTT_SIM_PASSWORD OG_MQTT_SIMCTL_PASSWORD OG_MQTT_ENGINE_PASSWORD \
         OG_MQTT_GUARDIAN_PASSWORD OG_MQTT_SAFESTOP_PASSWORD OG_MQTT_API_PASSWORD; do
    require_var "$v"
done

mosquitto_passwd -b -c "$OUT" og_sim "$OG_MQTT_SIM_PASSWORD"
mosquitto_passwd -b "$OUT" og_simctl "$OG_MQTT_SIMCTL_PASSWORD"
mosquitto_passwd -b "$OUT" og_engine "$OG_MQTT_ENGINE_PASSWORD"
mosquitto_passwd -b "$OUT" og_guardian "$OG_MQTT_GUARDIAN_PASSWORD"
mosquitto_passwd -b "$OUT" og_safestop "$OG_MQTT_SAFESTOP_PASSWORD"
mosquitto_passwd -b "$OUT" og_api "$OG_MQTT_API_PASSWORD"

chmod 600 "$OUT"
echo "gen-mosquitto-passwd.sh: wrote $OUT for 6 users"
