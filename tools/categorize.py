#!/usr/bin/env python3
"""
Fold the 34 legacy Aronium groups into a small set of till categories.

The legacy groups mix brands (Sabritas, Doritos, Gamesa) with categories
(Desechable, Medicamentos), so a cashier hunting for Cheetos has to know which
scheme it was filed under. This maps everything onto categories a person would
actually reach for, and derives a "Frecuentes" tab from real sales volume.

    python3 tools/categorize.py data/catalogue.json backup-from-windows/pos.db

Writes the categories back into the catalogue and fails loudly if any product
would be left without a home.
"""

import argparse
import json
import re
import sqlite3
import sys
import unicodedata
from collections import defaultdict

# Ordered as they should appear in the till's category rail.
CATEGORIES = [
    ("frecuentes", "Frecuentes"),      # derived from sales volume, not a legacy group
    ("cerveza", "Cerveza"),
    ("refrescos", "Refrescos"),
    ("aguas", "Aguas y Jugos"),
    ("botanas", "Botanas"),
    ("galletas", "Galletas"),
    ("dulces", "Dulces"),
    ("desechables", "Desechables"),
    ("abarrotes", "Abarrotes"),
    ("higiene", "Higiene y Farmacia"),
    ("playa", "Salvavidas y Playa"),
    ("otros", "Otros"),
]

# legacy group name -> category
GROUP_MAP = {
    "corona": "cerveza", "modelo": "cerveza", "cerveza": "cerveza",
    "coca-cola": "refrescos", "fresca": "refrescos", "fanta": "refrescos",
    "sidral mundet": "refrescos", "sprite": "refrescos",
    "ciel": "aguas", "bonafont": "aguas", "jumex": "aguas",
    "del valle": "aguas", "powerade": "aguas", "refrescos": "aguas",
    "sabritas": "botanas", "doritos": "botanas", "ruffles": "botanas",
    "cheetos": "botanas",
    "gamesa": "galletas", "emperador": "galletas", "chokis": "galletas",
    "cremax": "galletas", "floretinas": "galletas",
    "dulces": "dulces", "paletas": "dulces",
    "desechable": "desechables",
    "consumibles": "abarrotes",
    "higiene personal": "higiene", "medicamentos": "higiene",
    "salvavidas": "playa", "sandalias": "playa",
    "otro": "otros",
}

# Name rules for products whose legacy group is missing or misleading.
# Checked in order, first match wins, and they override GROUP_MAP.
NAME_RULES = [
    (r"\bhielo\b|\bcarbon\b",                        "otros"),
    (r"pistola de agua|salvavidas|sandalia|acuatica|flotador", "playa"),
    (r"corona|modelo|cerveza|new mix|victoria|tecate", "cerveza"),
    (r"coca.?cola|fanta|sprite|fresca|squirt|sidral|mundet", "refrescos"),
    (r"peafiel|pe;afiel|jumex|del valle|powerade|ciel|bonafont|jugo|nectar|fuzetea|flashlyte|agua|vitaminwater|gatorade",
                                                     "aguas"),
    (r"\bcafe\b|atun|clamato|salsa|cerillo|maruchan|sopa|azucar|sal\b", "abarrotes"),
    (r"cacahuate|sabritas|doritos|ruffles|cheetos|fritos|churrumais|rancheritos|paketaxo",
                                                     "botanas"),
    (r"galleta|emperador|chokis|cremax|florentina|floretina|saladitas", "galletas"),
    (r"paleta|gomita|dulce|pikaros|chicle",          "dulces"),
    (r"vaso|plato|cubierto|servilleta|desechable|popote", "desechables"),
    (r"papel higienico|panal|jabon|shampoo|tampax|toalla", "higiene"),
    (r"paracetamol|naproxeno|buscapina|itamol|melox|alka", "higiene"),
]

TOP_N_FREQUENT = 14


def norm(s):
    s = unicodedata.normalize("NFKD", (s or "").strip())
    return "".join(c for c in s if not unicodedata.combining(c)).casefold()


def classify(product, legacy_group):
    name = norm(product["name"])
    for pattern, cat in NAME_RULES:
        if re.search(pattern, name):
            return cat
    return GROUP_MAP.get(norm(legacy_group), None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("catalogue")
    ap.add_argument("db", help="pos.db, for sales volume")
    args = ap.parse_args()

    cat = json.load(open(args.catalogue, encoding="utf-8"))
    legacy = {g["id"]: g["name"] for g in cat["groups"]}

    # --- sales volume, for the Frecuentes tab ---
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    sold = {pid: float(q or 0) for pid, q in con.execute(
        "SELECT ProductId, SUM(Quantity) FROM DocumentItem "
        "WHERE ProductId IS NOT NULL GROUP BY ProductId")}

    unmapped = []
    counts = defaultdict(int)
    for p in cat["products"]:
        c = classify(p, legacy.get(p["group_id"], ""))
        if c is None:
            unmapped.append(p["name"])
            c = "otros"
        p["category"] = c
        p["sold_qty"] = sold.get(p["id"], 0.0)
        counts[c] += 1

    # Frecuentes is a view over the others, not an exclusive home: a product
    # keeps its real category and is additionally flagged.
    ranked = sorted((p for p in cat["products"] if p["is_active"]),
                    key=lambda p: -p["sold_qty"])[:TOP_N_FREQUENT]
    frequent_ids = {p["id"] for p in ranked if p["sold_qty"] > 0}
    for p in cat["products"]:
        p["is_frequent"] = p["id"] in frequent_ids

    cat["categories"] = [{"id": cid, "name": name} for cid, name in CATEGORIES]
    json.dump(cat, open(args.catalogue, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    print(f"categorised {len(cat['products'])} products into {len(CATEGORIES)} categories\n")
    for cid, name in CATEGORIES:
        if cid == "frecuentes":
            print(f"  {name:<20} {len(frequent_ids):>3}  (derived from sales volume)")
        else:
            print(f"  {name:<20} {counts[cid]:>3}")

    print("\n  Frecuentes tab:")
    for p in ranked:
        if p["sold_qty"] > 0:
            print(f"    {p['sold_qty']:>7.0f} sold   ${p['price']:>6.2f}  {p['name']}")

    if unmapped:
        print(f"\n  !! {len(unmapped)} products fell through to Otros:")
        for n in unmapped:
            print(f"     {n}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
