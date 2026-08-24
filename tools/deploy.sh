#!/usr/bin/env bash
# Push the register app to the till and restart it.
#   tools/deploy.sh [--seed]
set -euo pipefail
HOST="${REGISTER_HOST:-cashregister}"
cd "$(dirname "$0")/.."

tar czf - --exclude='__pycache__' --exclude='*.pyc' \
    -C register app schema.sql seed.py manage.py smoketest.py requirements.txt \
  | ssh "$HOST" 'sudo -n tar xzf - -C /opt/cashregister && sudo -n chown -R tienda:tienda /opt/cashregister'

if [[ "${1:-}" == "--seed" ]]; then
  scp -q data/catalogue.json "$HOST":/tmp/catalogue.json
  ssh "$HOST" 'sudo -n -u tienda /opt/cashregister/venv/bin/python /opt/cashregister/seed.py \
      --catalogue /tmp/catalogue.json --db /var/lib/cashregister/register.db \
      --schema /opt/cashregister/schema.sql'
fi

ssh "$HOST" 'sudo -n systemctl restart cashregister.service && sleep 2 &&
             systemctl is-active cashregister.service | sed "s/^/app: /" &&
             sudo -n systemctl restart cashregister-kiosk.service && sleep 4 &&
             systemctl is-active cashregister-kiosk.service | sed "s/^/kiosk: /"'
