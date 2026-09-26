set -euo pipefail
ACL=/etc/mosquitto/opengrid.acl
cp "$ACL" "$ACL.bak.$(date +%Y%m%d%H%M%S)"
grep -v '^pattern readwrite ogtest/#$' "$ACL" > "$ACL.new"
mv "$ACL.new" "$ACL"
chown root:mosquitto "$ACL" 2>/dev/null || chown root:root "$ACL"
chmod 640 "$ACL"
echo "=== new ACL ==="
cat "$ACL"
echo "=== restarting mosquitto ==="
systemctl restart mosquitto
sleep 3
systemctl is-active mosquitto
echo "=== waiting for og-*/ogsim MQTT clients to reconnect on their own ==="
sleep 10
echo "=== unit status (all should still be active running, no restarts needed) ==="
systemctl list-units 'og-*' --all --no-pager
