#!/usr/bin/env python3
"""
Create a register database and load the catalogue into it.

    python3 register/seed.py --catalogue data/catalogue.json --db register/register.db

Safe to re-run: the catalogue is upserted, and sales/shifts/audit are never
touched. That makes this the normal way to apply a price change until the
backend (Phase 5) can push one.
"""

import argparse
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone

SCHEMA_VERSION = "1"
HERE = os.path.dirname(os.path.abspath(__file__))


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_cents(v):
    """Money arrives as a float from JSON; it must never stay one."""
    if v is None:
        return None
    return int(round(float(v) * 100))


def normalise_code(code):
    """
    Reduce a scanned or stored code to its canonical identity.

    UPC-A is EAN-13 with an implied leading zero — a US import scanned as 12
    digits must match the same product stored as 13, or it silently fails to
    ring up. EAN-8 is its own thing and is left alone.
    """
    code = (code or "").strip()
    if code.isdigit() and len(code) == 12:
        return "0" + code
    return code


def connect(db_path, schema_path):
    fresh = not os.path.exists(db_path)
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    if fresh:
        with open(schema_path, encoding="utf-8") as f:
            con.executescript(f.read())
    return con, fresh


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalogue", default="data/catalogue.json")
    ap.add_argument("--db", default="register/register.db")
    ap.add_argument("--schema", default=os.path.join(HERE, "schema.sql"))
    ap.add_argument("--register-id", help="stable id for this till; generated once if absent")
    args = ap.parse_args()

    cat = json.load(open(args.catalogue, encoding="utf-8"))
    if "categories" not in cat:
        sys.exit("catalogue has no categories — run tools/categorize.py first")

    con, fresh = connect(args.db, args.schema)
    ts = now_iso()

    def meta_get(key, default=None):
        row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def meta_set(key, value):
        con.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, str(value)))

    register_id = args.register_id or meta_get("register_id") or str(uuid.uuid4())
    meta_set("register_id", register_id)
    meta_set("schema_version", SCHEMA_VERSION)

    # ---- categories ----
    for i, c in enumerate(cat["categories"]):
        con.execute("INSERT INTO category(id, name, sort_order) VALUES(?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET name = excluded.name, "
                    "sort_order = excluded.sort_order",
                    (c["id"], c["name"], i))

    # ---- products + barcodes ----
    n_prod = n_code = 0
    conflicts = []
    for p in cat["products"]:
        con.execute(
            "INSERT INTO product(id, category_id, name, price_cents, cost_cents, "
            "                    is_active, is_frequent, sort_hint, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET category_id = excluded.category_id, "
            "  name = excluded.name, price_cents = excluded.price_cents, "
            "  cost_cents = excluded.cost_cents, is_active = excluded.is_active, "
            "  is_frequent = excluded.is_frequent, sort_hint = excluded.sort_hint, "
            "  updated_at = excluded.updated_at",
            (p["id"], p["category"], p["name"], to_cents(p["price"]), to_cents(p["cost"]),
             1 if p["is_active"] else 0, 1 if p.get("is_frequent") else 0,
             int(p.get("sold_qty", 0)), ts))
        n_prod += 1

        for b in p["barcodes"]:
            raw = b["code"]
            code = normalise_code(raw)
            owner = con.execute("SELECT product_id FROM barcode WHERE code = ?",
                                (code,)).fetchone()
            if owner and owner[0] != p["id"]:
                # Never silently reassign: that is how a scan rings up the wrong item.
                conflicts.append((code, owner[0], p["id"]))
                continue
            con.execute("INSERT INTO barcode(code, product_id, is_internal, printed) "
                        "VALUES(?, ?, ?, ?) ON CONFLICT(code) DO UPDATE SET "
                        "product_id = excluded.product_id, is_internal = excluded.is_internal",
                        (code, p["id"], 1 if b.get("internal") else 0,
                         raw if raw != code else None))
            n_code += 1

    meta_set("catalogue_revision", int(meta_get("catalogue_revision", 0)) + 1)
    con.commit()

    print(f"{'created' if fresh else 'updated'} {args.db}")
    print(f"  register_id        {register_id}")
    print(f"  catalogue_revision {meta_get('catalogue_revision')}")
    print(f"  categories         {len(cat['categories'])}")
    print(f"  products           {n_prod}")
    print(f"  barcodes           {n_code}")
    padded = con.execute("SELECT COUNT(*) FROM barcode WHERE printed IS NOT NULL").fetchone()[0]
    if padded:
        print(f"    of which UPC-A normalised to 13 digits: {padded}")
    if conflicts:
        print(f"\n  !! {len(conflicts)} barcode conflicts, left pointing at the FIRST product:")
        for code, kept, rejected in conflicts:
            print(f"     {code}: kept product {kept}, rejected {rejected}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
