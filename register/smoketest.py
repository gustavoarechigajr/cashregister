#!/usr/bin/env python3
"""
End-to-end check against a running register. Read-mostly, but it DOES commit one
real sale — run it against a till that is not in service, or accept a test ticket
in the day's numbering.

    python3 smoketest.py [--base http://127.0.0.1:8080] --pin 1234 --user 1
"""
import argparse, json, sys, urllib.request, http.cookiejar

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--user", type=int, default=1)
    ap.add_argument("--pin", default="1234")
    ap.add_argument("--commit-sale", action="store_true", help="actually ring up a sale")
    a = ap.parse_args()

    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def call(path, body=None):
        req = urllib.request.Request(a.base + path,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"},
                                     method="POST" if body is not None else "GET")
        with op.open(req, timeout=10) as r:
            return json.loads(r.read() or b"{}")

    ok = fail = 0
    def check(label, cond, extra=""):
        nonlocal ok, fail
        if cond: ok += 1;  print(f"  PASS  {label} {extra}")
        else:    fail += 1; print(f"  FAIL  {label} {extra}")

    b = call("/api/bootstrap")
    check("bootstrap", b["catalogue"]["products"], f"{len(b['catalogue']['products'])} products, "
                                                   f"{len(b['catalogue']['categories'])} categories")
    check("users present", len(b["users"]) >= 2, f"{len(b['users'])}")
    freq = [p for p in b["catalogue"]["products"] if p["is_frequent"]]
    check("frecuentes populated", len(freq) > 0, f"{len(freq)} items")

    s = call("/api/login", {"user_id": a.user, "pin": a.pin})
    check("login", s["session"]["name"], s["session"]["name"])

    b = call("/api/bootstrap")
    if not b["shift"]:
        call("/api/shift/open", {"opening_float_cents": 150000})
        b = call("/api/bootstrap")
    check("shift open", b["shift"] is not None,
          f"float ${b['shift']['opening_float_cents']/100:,.2f}" if b["shift"] else "")

    p = call("/api/scan?code=7501055308248")
    check("scan EAN-13", p["name"].startswith("Ciel"), f"{p['name']} ${p['price_cents']/100:.2f}")
    p2 = call("/api/scan?code=041789001956")
    check("scan UPC-A (12-digit normalised)", p2 is not None, p2["name"])
    try:
        call("/api/scan?code=0000000000000"); check("unknown barcode rejected", False)
    except urllib.error.HTTPError as e:
        check("unknown barcode rejected", e.code == 404, "404")

    if a.commit_sale:
        r = call("/api/sale", {"lines": [{"product_id": p["id"], "qty": 2}],
                               "tendered_cents": 10000})
        expected = p["price_cents"] * 2
        check("sale total", r["total_cents"] == expected, f"ticket #{r['seq']} {r['total']}")
        check("change", r["change_cents"] == 10000 - expected, r["change"])
        try:
            call("/api/sale", {"lines": [{"product_id": p["id"], "qty": 1}], "tendered_cents": 1})
            check("underpayment rejected", False)
        except urllib.error.HTTPError as e:
            check("underpayment rejected", e.code == 400, "400")
        b = call("/api/bootstrap")
        check("outbox filling", b["outbox_pending"] > 0, f"{b['outbox_pending']} pending")

    print(f"\n  {ok} passed, {fail} failed")
    return 1 if fail else 0

if __name__ == "__main__":
    sys.exit(main())
