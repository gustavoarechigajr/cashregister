#!/usr/bin/env bash
# Boot to console instead of GNOME, and install the kiosk unit. Run as root.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get -y -qq install cage chromium sqlite3 python3-venv python3-pip

# Boot to a console. GNOME stays installed but never starts.
systemctl set-default multi-user.target
systemctl disable gdm.service 2>/dev/null || true
systemctl stop    gdm.service 2>/dev/null || true

install -d -m 755 -o tienda -g tienda /opt/cashregister
install -d -m 755 -o tienda -g tienda /var/lib/cashregister
install -d -m 755 -o tienda -g tienda /var/backups/cashregister

install -m 644 "$(dirname "$0")/cashregister-kiosk.service" \
        /etc/systemd/system/cashregister-kiosk.service
systemctl daemon-reload

echo "kiosk unit installed but NOT enabled — enable once the app serves 127.0.0.1:8080:"
echo "  systemctl enable --now cashregister-kiosk.service"
