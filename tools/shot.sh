#!/usr/bin/env bash
# Screenshot the till's real screen, waiting until it has actually painted.
#   tools/shot.sh [outfile]
set -euo pipefail
HOST="${REGISTER_HOST:-cashregister}"
OUT="${1:-/tmp/claude-1000/shots/screen.png}"
mkdir -p "$(dirname "$OUT")"

for attempt in $(seq 1 12); do
  ssh "$HOST" 'sudo -n -u tienda env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0 \
               grim /tmp/screen.png' >/dev/null
  scp -q "$HOST":/tmp/screen.png "$OUT"
  # A blank splash compresses to almost nothing; a rendered till does not.
  size=$(stat -c%s "$OUT")
  if [ "$size" -gt 25000 ]; then
    echo "$OUT (${size} bytes, attempt ${attempt})"
    exit 0
  fi
  sleep 3
done
echo "$OUT (still blank after 12 attempts — check the kiosk journal)" >&2
exit 1
