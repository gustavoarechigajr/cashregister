"""
One-time seed of the catalogue from a register into the central database.

Direction note: this runs register -> central, which is the OPPOSITE of the
eventual model. Central is meant to own the catalogue and push it down
(PLAN.md). But the till has been the only master since Phase 1 and holds the
real data, so central has to be seeded from it once before ownership can flip.

Idempotent: re-running updates rows rather than duplicating them, so it can be
run again safely while the till is still the master.

Usage, from a machine that can reach both:
    python3 seed_catalogue.py --register gus@10.0.0.22 --dsn "host=10.0.0.16 ..."
or on the backend, pointing at a JSON dump produced on the till.
"""

import argparse
import json
import shlex
import subprocess
import sys

DUMP_SQL = r"""
select json_object(
  'categories', (select json_group_array(json_object('id', id, 'name', name))
                   from (select id, name from category order by sort_order, name)),
  'products',   (select json_group_array(json_object(
                    'id', id, 'category_id', category_id, 'name', name,
                    'price_cents', price_cents, 'cost_cents', cost_cents,
                    'is_active', is_active))
                   from (select * from product order by id)),
  'barcodes',   (select json_group_array(json_object(
                    'code', code, 'product_id', product_id, 'is_internal', is_internal))
                   from barcode)
);
"""


def fetch_from_register(target, db="/var/lib/cashregister/register.db"):
    out = subprocess.run(
        # shlex.quote, not json.dumps: JSON escaping turns the newlines in the
        # query into literal backslash-n, which sqlite rejects as a stray token.
        ["ssh", "-o", "BatchMode=yes", target,
         "sudo sqlite3 %s %s" % (db, shlex.quote(" ".join(DUMP_SQL.split())))],
        capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        sys.exit("register dump failed: " + out.stderr.strip()[:300])
    return json.loads(out.stdout.strip())


def seed(dsn, cat):
    # Imported here, not at module scope: the dump half of this script runs
    # on a machine that has ssh but not psycopg2.
    import psycopg2
    con = psycopg2.connect(dsn)
    # Set explicitly: under LC_ALL=C libpq negotiates SQL_ASCII and every
    # n-tilde in the catalogue fails to encode.
    con.set_client_encoding("UTF8")
    cur = con.cursor()
    cur.executemany(
        "INSERT INTO category (id, name) VALUES (%(id)s, %(name)s) "
        "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name",
        cat["categories"])
    cur.executemany(
        "INSERT INTO product (id, category_id, name, price_cents, cost_cents, is_active) "
        "VALUES (%(id)s, %(category_id)s, %(name)s, %(price_cents)s, %(cost_cents)s,"
        "        %(is_active)s::int::boolean) "
        "ON CONFLICT (id) DO UPDATE SET category_id = EXCLUDED.category_id,"
        "  name = EXCLUDED.name, price_cents = EXCLUDED.price_cents,"
        "  cost_cents = EXCLUDED.cost_cents, is_active = EXCLUDED.is_active,"
        "  updated_at = now()",
        cat["products"])
    cur.executemany(
        "INSERT INTO barcode (code, product_id, is_internal) "
        "VALUES (%(code)s, %(product_id)s, %(is_internal)s::int::boolean) "
        "ON CONFLICT (code) DO UPDATE SET product_id = EXCLUDED.product_id,"
        "  is_internal = EXCLUDED.is_internal",
        cat["barcodes"])
    con.commit()
    for t in ("category", "product", "barcode"):
        cur.execute("select count(*) from " + t)
        print("  %-9s %d" % (t, cur.fetchone()[0]))
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--register", default="gus@10.0.0.22")
    ap.add_argument("--json", help="read a dump produced earlier instead of ssh-ing")
    ap.add_argument("--dsn", default="dbname=caja user=caja host=/var/run/postgresql")
    a = ap.parse_args()
    # --json exists because the machine with psycopg2 (the backend) is not
    # necessarily the machine that can ssh to the till.
    data = json.load(open(a.json)) if a.json else fetch_from_register(a.register)
    print("fetched: %d categories, %d products, %d barcodes" % (
        len(data["categories"]), len(data["products"]), len(data["barcodes"])))
    seed(a.dsn, data)
