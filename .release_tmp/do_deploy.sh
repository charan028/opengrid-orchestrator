set -euo pipefail
TS="$(date +%Y%m%d%H%M%S)"
DIR="/root/release-$TS"
mkdir -p "$DIR"
tar -xzf /root/release.tgz -C "$DIR"
rm -f /root/release.tgz
echo "extracted to $DIR"
bash /opt/opengrid/deploy/scripts/deploy.sh "$DIR"
