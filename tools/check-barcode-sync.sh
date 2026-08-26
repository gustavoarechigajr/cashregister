#!/usr/bin/env bash
# Are the till's barcodes and central's in agreement?
#
# The till owns barcodes and pushes them up, so a mismatch means the push has
# not landed yet -- give it a minute -- or something is wrong. Read-only: it
# only ever SELECTs from either side.
#
#   tools/check-barcode-sync.sh
#   REGISTER_HOST=gus@10.0.50.101 tools/check-barcode-sync.sh   # while on Wi-Fi
set -euo pipefail
TILL="${REGISTER_HOST:-cashregister}"
CENTRAL="${CENTRAL_HOST:-root@10.0.0.16}"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

ssh "$TILL" "sudo -n -u tienda sqlite3 -separator '|' /var/lib/cashregister/register.db \
    'SELECT code, product_id FROM barcode ORDER BY code'" > "$tmp/till.txt"
ssh "$CENTRAL" "sudo -u postgres psql -d caja -tAF'|' \
    -c 'SELECT code, product_id FROM barcode ORDER BY code' 2>/dev/null" \
  | grep '|' > "$tmp/central.txt"

python3 - "$tmp/till.txt" "$tmp/central.txt" <<'PY'
import sys
def load(p):
    d = {}
    for line in open(p):
        line = line.strip()
        if line and '|' in line:
            code, pid = line.split('|')
            d[code.strip()] = pid.strip()
    return d
till, central = load(sys.argv[1]), load(sys.argv[2])
only_till = sorted(set(till) - set(central))
only_cen  = sorted(set(central) - set(till))
moved     = sorted(k for k in set(till) & set(central) if till[k] != central[k])
print(f"caja: {len(till)} codigos   central: {len(central)} codigos")
for label, rows in (("sin subir (solo en la caja)", only_till),
                    ("fantasma (solo en central)", only_cen)):
    print(f"\n{label}: {len(rows)}")
    for c in rows[:20]:
        print(f"   {c} -> {(till if rows is only_till else central)[c]}")
    if len(rows) > 20:
        print(f"   ... y {len(rows)-20} mas")
print(f"\napuntan a producto distinto: {len(moved)}")
for c in moved[:20]:
    print(f"   {c}  caja={till[c]}  central={central[c]}")
ok = not (only_till or only_cen or moved)
print("\n>>> SINCRONIZADO" if ok else "\n>>> NO SINCRONIZADO")
sys.exit(0 if ok else 1)
PY
