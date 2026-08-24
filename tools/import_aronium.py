#!/usr/bin/env python3
"""
Import the catalogue out of the recovered Aronium database.

Reads the read-only pos.db backup and emits a cleaned catalogue plus a report
of everything that needed fixing or still needs a human decision.

    python3 tools/import_aronium.py backup-from-windows/pos.db -o data/catalogue.json

Nothing here writes to pos.db. Cleaning is conservative: obvious junk is dropped,
ambiguous problems are flagged rather than guessed at.
"""

import argparse
import json
import re
import sqlite3
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone

TEST_PRODUCT = re.compile(r"^\s*test\s*\d*\s*$", re.I)
# a barcode fat-fingered into the end of a product name, e.g.
# "salvavidas fucsia 8 a6941057452586"
TRAILING_BARCODE = re.compile(r"\s+a?(\d{12,14})\s*$")


def norm_key(s):
    """Casefold + strip accents, for detecting duplicates that differ cosmetically."""
    s = unicodedata.normalize("NFKD", (s or "").strip())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).casefold()


def tidy(s):
    return re.sub(r"\s+", " ", (s or "").strip())


def ean_check_ok(code):
    """Validate an EAN-8 / UPC-A / EAN-13 check digit. None if not a checkable length."""
    if not code.isdigit():
        return None
    if len(code) == 8:           # EAN-8: weights 3,1,3,1,3,1,3
        total = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(code[:7]))
        return (10 - total % 10) % 10 == int(code[7])
    if len(code) == 12:          # UPC-A -> pad to EAN-13
        code = "0" + code
    if len(code) != 13:
        return None
    total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(code[:12]))
    return (10 - total % 10) % 10 == int(code[12])


# ñ/á typed through a bad keymap land as punctuation: "sandalia de ni;o"
SUSPECT_CHARS = re.compile(r"[;?¿\\^~`\uFFFD]|[A-Za-z][;^~][a-z]")


def load(db_path):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("-o", "--out", default="data/catalogue.json")
    args = ap.parse_args()

    con = load(args.db)
    issues = defaultdict(list)

    # ---- groups: merge duplicates that differ only by case/accents ----
    groups, canonical = {}, {}
    for r in con.execute("SELECT Id, Name, Rank FROM ProductGroup ORDER BY Id"):
        key = norm_key(r["Name"])
        if key in canonical:
            keep = canonical[key]
            canonical[r["Id"]] = keep
            issues["merged_groups"].append(
                {"dropped_id": r["Id"], "kept_id": keep, "name": tidy(r["Name"])})
        else:
            canonical[key] = r["Id"]
            canonical[r["Id"]] = r["Id"]
            groups[r["Id"]] = {"id": r["Id"], "name": tidy(r["Name"]),
                               "sort_order": r["Rank"] or 0}

    # ---- barcodes ----
    codes = defaultdict(list)
    seen = {}
    for r in con.execute("SELECT ProductId, Value FROM Barcode"):
        code = tidy(r["Value"])
        if not code:
            continue
        if code in seen and seen[code] != r["ProductId"]:
            issues["duplicate_barcode"].append(
                {"code": code, "products": [seen[code], r["ProductId"]]})
            continue
        seen[code] = r["ProductId"]
        ok = ean_check_ok(code)
        if ok is False:
            issues["bad_check_digit"].append({"product_id": r["ProductId"], "code": code})
        codes[r["ProductId"]].append({"code": code, "internal": code.startswith("2"),
                                      "checksum_ok": ok})

    # ---- sold-ever, for spotting dead entries ----
    sold = {r[0] for r in con.execute(
        "SELECT DISTINCT ProductId FROM DocumentItem WHERE ProductId IS NOT NULL")}

    # ---- stock ----
    stock = {r["ProductId"]: float(r["Quantity"] or 0)
             for r in con.execute("SELECT ProductId, Quantity FROM Stock")}

    products = []
    for r in con.execute("SELECT * FROM Product ORDER BY Id"):
        pid, name = r["Id"], tidy(r["Name"])
        price = float(r["Price"] or 0)
        cost = None if r["Cost"] in (None, 0) else float(r["Cost"])
        enabled = bool(r["IsEnabled"])

        # drop obvious test rows
        if TEST_PRODUCT.match(name) and not enabled:
            issues["dropped_test_rows"].append({"id": pid, "name": name})
            continue

        # A barcode typed into the name field. This happens because the scanner is
        # a keyboard wedge: it types into whatever has focus, so scanning while the
        # cursor sits in "name" creates a junk product named after the barcode.
        m = TRAILING_BARCODE.search(name)
        if m:
            found = m.group(1)
            name = tidy(name[: m.start()])
            owner = seen.get(found)
            if owner is not None and owner != pid:
                # The code already belongs to a real product — this row is the
                # accident, not the record. Deactivate rather than delete, and
                # never steal the barcode.
                issues["scan_typed_into_name"].append(
                    {"id": pid, "name": name, "code": found, "real_product_id": owner})
                enabled = False
            else:
                issues["barcode_in_name"].append({"id": pid, "name": name, "code": found})
                if not any(c["code"] == found for c in codes[pid]):
                    codes[pid].append({"code": found, "internal": found.startswith("2"),
                                       "checksum_ok": ean_check_ok(found)})
                    seen[found] = pid

        if SUSPECT_CHARS.search(name):
            issues["suspect_characters"].append({"id": pid, "name": name})
        if enabled and price <= 0:
            issues["zero_price_but_active"].append({"id": pid, "name": name})
        if cost is None:
            issues["no_cost"].append({"id": pid, "name": name})
        elif cost > price > 0:
            issues["cost_above_price"].append({"id": pid, "name": name,
                                               "cost": cost, "price": price})
        if not codes[pid]:
            issues["no_barcode"].append({"id": pid, "name": name})
        if pid not in sold:
            issues["never_sold"].append({"id": pid, "name": name})

        products.append({
            "id": pid,
            "group_id": canonical.get(r["ProductGroupId"], r["ProductGroupId"]),
            "name": name,
            "price": round(price, 2),
            "cost": round(cost, 2) if cost is not None else None,
            "is_active": enabled and price > 0,
            "barcodes": codes[pid],
            "stock_on_hand": stock.get(pid, 0.0),
            # Aronium's MeasurementUnit is free text with ~80 inconsistent spellings
            # ("600 ml" vs "600ml"). Kept for reference only — never authoritative.
            "legacy_unit": tidy(r["MeasurementUnit"]) or None,
        })

    # duplicate product names, after cleaning
    by_name = defaultdict(list)
    for p in products:
        by_name[norm_key(p["name"])].append(p["id"])
    for key, ids in by_name.items():
        if len(ids) > 1:
            issues["duplicate_name"].append({"ids": ids, "name": key})

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": args.db,
        "groups": sorted(groups.values(), key=lambda g: g["name"]),
        "products": products,
    }

    import os
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    issues_path = os.path.splitext(args.out)[0] + "-issues.json"
    with open(issues_path, "w", encoding="utf-8") as f:
        json.dump(dict(issues), f, ensure_ascii=False, indent=2)

    # ---- report ----
    active = sum(1 for p in products if p["is_active"])
    print(f"wrote {args.out}")
    print(f"  groups   : {len(groups)} (merged {len(issues['merged_groups'])} duplicates)")
    print(f"  products : {len(products)}  ({active} active)")
    print(f"  barcodes : {sum(len(p['barcodes']) for p in products)}")
    print()
    order = ["merged_groups", "scan_typed_into_name", "barcode_in_name",
             "zero_price_but_active",
             "duplicate_barcode", "bad_check_digit", "suspect_characters",
             "duplicate_name", "cost_above_price", "no_barcode", "no_cost",
             "never_sold", "dropped_test_rows"]
    for k in order:
        v = issues.get(k)
        if v:
            print(f"  {k:<22} {len(v)}")
    blocking = [k for k in ("scan_typed_into_name", "zero_price_but_active",
                            "duplicate_barcode",
                            "bad_check_digit", "cost_above_price",
                            "suspect_characters") if issues.get(k)]
    if blocking:
        print("\n  NEEDS A HUMAN DECISION:")
        for k in blocking:
            for item in issues[k][:10]:
                print(f"    {k}: {item}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
