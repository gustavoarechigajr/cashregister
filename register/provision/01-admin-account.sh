#!/usr/bin/env bash
# Create the administration account. Run as root on a fresh install.
#   ADMIN_KEY='ssh-ed25519 AAAA...' bash 01-admin-account.sh
set -euo pipefail
: "${ADMIN_KEY:?set ADMIN_KEY to the admin SSH public key}"
ADMIN_USER="${ADMIN_USER:-gus}"

id "$ADMIN_USER" >/dev/null 2>&1 || \
  adduser --disabled-password --gecos "administrator" "$ADMIN_USER"
usermod -aG sudo "$ADMIN_USER"

install -d -m 700 -o "$ADMIN_USER" -g "$ADMIN_USER" "/home/$ADMIN_USER/.ssh"
echo "$ADMIN_KEY" > "/home/$ADMIN_USER/.ssh/authorized_keys"
chown "$ADMIN_USER:$ADMIN_USER" "/home/$ADMIN_USER/.ssh/authorized_keys"
chmod 600 "/home/$ADMIN_USER/.ssh/authorized_keys"

# key possession is the authentication for this account
echo "$ADMIN_USER ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/90-$ADMIN_USER"
chmod 440 "/etc/sudoers.d/90-$ADMIN_USER"
visudo -c -f "/etc/sudoers.d/90-$ADMIN_USER"

echo "created:"; id "$ADMIN_USER"
