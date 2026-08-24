#!/usr/bin/env python3
"""
Register administration from the command line.

    python3 manage.py users
    python3 manage.py adduser "Rosa Mendoza" cashier
    python3 manage.py setpin 1
    python3 manage.py unlock 1
    python3 manage.py status

PINs are never passed as arguments — they would land in shell history and in the
process list. They are prompted for, twice, without echo.
"""
import argparse, getpass, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import auth, db, money


def ask_pin(length_hint):
    a = getpass.getpass(f"PIN ({length_hint} digits): ")
    b = getpass.getpass("repeat: ")
    if a != b:
        sys.exit("PINs do not match")
    if not a.isdigit() or not (4 <= len(a) <= 12):
        sys.exit("PIN must be 4-12 digits")
    return a


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("users")
    sub.add_parser("status")
    a = sub.add_parser("adduser"); a.add_argument("name"); a.add_argument("role", choices=["admin", "cashier"])
    s = sub.add_parser("setpin"); s.add_argument("user_id", type=int)
    u = sub.add_parser("unlock"); u.add_argument("user_id", type=int)
    args = ap.parse_args()
    con = db.connect()

    if args.cmd == "users":
        for r in con.execute("SELECT id, name, role, is_active, failed_attempts, locked_until FROM app_user ORDER BY id"):
            lock = f"  LOCKED until {r['locked_until']}" if r["locked_until"] else ""
            print(f"  {r['id']:>3}  {r['role']:<8} {r['name']:<24}{lock}")

    elif args.cmd == "adduser":
        pin = ask_pin(6 if args.role == "admin" else 4)
        cur = con.execute("INSERT INTO app_user(name, role, pin_hash, updated_at) VALUES(?,?,?,?)",
                          (args.name, args.role, auth.hash_pin(pin), db.now_iso()))
        print(f"created user {cur.lastrowid}: {args.name} ({args.role})")

    elif args.cmd == "setpin":
        row = con.execute("SELECT name, role FROM app_user WHERE id = ?", (args.user_id,)).fetchone()
        if not row:
            sys.exit("no such user")
        pin = ask_pin(6 if row["role"] == "admin" else 4)
        con.execute("UPDATE app_user SET pin_hash = ?, failed_attempts = 0, locked_until = NULL, "
                    "updated_at = ? WHERE id = ?", (auth.hash_pin(pin), db.now_iso(), args.user_id))
        print(f"PIN updated for {row['name']}")

    elif args.cmd == "unlock":
        con.execute("UPDATE app_user SET failed_attempts = 0, locked_until = NULL WHERE id = ?",
                    (args.user_id,))
        print("unlocked")

    elif args.cmd == "status":
        sh = db.current_shift(con)
        print(f"  register   {db.meta(con, 'register_id')}")
        print(f"  catalogue  revision {db.meta(con, 'catalogue_revision')}, "
              f"{con.execute('SELECT COUNT(*) c FROM product WHERE is_active=1').fetchone()['c']} active products")
        print(f"  tickets    {db.meta(con, 'ticket_seq', 0)} issued")
        print(f"  outbox     {db.outbox_pending(con)} pending")
        if sh:
            print(f"  shift      open since {sh['opened_at']}, expected "
                  f"{money.format_mxn(db.shift_expected_cents(con, sh['id']))}")
        else:
            print("  shift      none open")


if __name__ == "__main__":
    main()
