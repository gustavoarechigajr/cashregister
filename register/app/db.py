"""
Database access for the register.

Everything that changes money happens inside one transaction, and every such
change also writes its sync_outbox row in that same transaction. If the commit
fails there is no half-sale and no orphaned outbox entry.
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta

from . import auth, barcode as bc

DB_PATH = os.environ.get("CASHREGISTER_DB", "/var/lib/cashregister/register.db")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(path or DB_PATH, isolation_level=None, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = FULL")
    return con


def meta(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


# --------------------------------------------------------------- catalogue

def catalogue(con) -> dict:
    cats = [dict(r) for r in con.execute(
        "SELECT id, name FROM category ORDER BY sort_order, name")]
    prods = [dict(r) for r in con.execute(
        "SELECT id, category_id, name, price_cents, is_frequent, sort_hint "
        "FROM product WHERE is_active = 1 "
        "ORDER BY sort_hint DESC, name")]
    return {"categories": cats, "products": prods,
            "revision": int(meta(con, "catalogue_revision", 0))}


def find_by_barcode(con, code: str):
    """Scan lookup. Normalises so a 12-digit UPC-A matches its 13-digit identity."""
    row = con.execute(
        "SELECT p.id, p.name, p.price_cents, p.category_id, p.is_active "
        "FROM barcode b JOIN product p ON p.id = b.product_id WHERE b.code = ?",
        (bc.normalise(code),)).fetchone()
    return dict(row) if row else None


# -------------------------------------------------------------------- auth

def users(con):
    return [dict(r) for r in con.execute(
        "SELECT id, name, role FROM app_user WHERE is_active = 1 ORDER BY role DESC, name")]


def authenticate(con, user_id: int, pin: str):
    """
    Returns (user_dict, None) or (None, reason).

    Lockout is the actual control on a 4-digit PIN, so it is enforced here and
    every failure is audited — a run of them is the signal that matters.
    """
    row = con.execute("SELECT * FROM app_user WHERE id = ? AND is_active = 1",
                      (user_id,)).fetchone()
    if row is None:
        return None, "unknown_user"

    if row["locked_until"] and row["locked_until"] > now_iso():
        return None, "locked"

    if not auth.verify_pin(pin, row["pin_hash"]):
        fails = row["failed_attempts"] + 1
        locked = None
        if fails >= auth.MAX_FAILURES:
            locked = (datetime.now(timezone.utc)
                      + timedelta(seconds=auth.LOCKOUT_SECONDS)).isoformat(timespec="seconds")
            fails = 0
        con.execute("UPDATE app_user SET failed_attempts = ?, locked_until = ? WHERE id = ?",
                    (fails, locked, user_id))
        audit(con, "login_failed", by_user=user_id,
              detail={"attempt": fails, "locked": bool(locked)})
        return None, "locked" if locked else "bad_pin"

    con.execute("UPDATE app_user SET failed_attempts = 0, locked_until = NULL WHERE id = ?",
                (user_id,))
    return {"id": row["id"], "name": row["name"], "role": row["role"]}, None


# ------------------------------------------------------------------ audit

def audit(con, action, by_user=None, authorized_by=None, detail=None):
    con.execute(
        "INSERT INTO audit_event(id, register_id, at, action, by_user, authorized_by, detail) "
        "VALUES(?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), meta(con, "register_id"), now_iso(), action,
         by_user, authorized_by, json.dumps(detail or {}, ensure_ascii=False)))


def _outbox(con, entity, entity_id, payload):
    con.execute(
        "INSERT INTO sync_outbox(entity, entity_id, payload, created_at) VALUES(?, ?, ?, ?)",
        (entity, entity_id, json.dumps(payload, ensure_ascii=False), now_iso()))


# ----------------------------------------------------------------- shifts

def open_shift(con, user_id: int, opening_float_cents: int) -> dict:
    existing = con.execute(
        "SELECT id FROM shift WHERE closed_at IS NULL ORDER BY opened_at DESC LIMIT 1").fetchone()
    if existing:
        return dict(con.execute("SELECT * FROM shift WHERE id = ?", (existing["id"],)).fetchone())

    sid, ts = str(uuid.uuid4()), now_iso()
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("INSERT INTO shift(id, register_id, user_id, opened_at, opening_float_cents) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (sid, meta(con, "register_id"), user_id, ts, opening_float_cents))
        _outbox(con, "shift", sid, {"id": sid, "user_id": user_id, "opened_at": ts,
                                    "opening_float_cents": opening_float_cents})
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return dict(con.execute("SELECT * FROM shift WHERE id = ?", (sid,)).fetchone())


def current_shift(con):
    row = con.execute("SELECT * FROM shift WHERE closed_at IS NULL "
                      "ORDER BY opened_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def shift_expected_cents(con, shift_id: str) -> int:
    row = con.execute("SELECT expected_cents FROM v_shift_expected WHERE shift_id = ?",
                      (shift_id,)).fetchone()
    return int(row["expected_cents"]) if row else 0


# ------------------------------------------------------------------ sales

def commit_sale(con, *, shift_id, user_id, lines, tendered_cents,
                kind="sale", refunds_sale_id=None, authorized_by=None) -> dict:
    """
    Write an immutable sale plus its lines and its outbox row, atomically.

    `lines` is [{product_id, qty}] — prices are re-read from the catalogue here
    rather than trusted from the client, then snapshotted onto the line so a
    later price change cannot rewrite what was charged.
    """
    if not lines:
        raise ValueError("a sale needs at least one line")

    ts = now_iso()
    sale_id = str(uuid.uuid4())
    register_id = meta(con, "register_id")

    con.execute("BEGIN IMMEDIATE")
    try:
        resolved, total = [], 0
        for item in lines:
            p = con.execute("SELECT id, name, price_cents FROM product WHERE id = ?",
                            (item["product_id"],)).fetchone()
            if p is None:
                raise ValueError(f"unknown product {item['product_id']}")
            qty = int(item["qty"])
            if qty <= 0:
                raise ValueError("qty must be positive")
            line_total = p["price_cents"] * qty
            total += line_total
            resolved.append({"product_id": p["id"], "name_at_sale": p["name"],
                             "unit_price_cents": p["price_cents"], "qty": qty,
                             "line_total_cents": line_total})

        if kind == "sale" and tendered_cents < total:
            raise ValueError("tendered is less than the total")

        seq = int(meta(con, "ticket_seq", 0)) + 1
        con.execute("INSERT INTO meta(key, value) VALUES('ticket_seq', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(seq),))

        change = tendered_cents - total if kind == "sale" else 0
        con.execute(
            "INSERT INTO sale(id, register_id, shift_id, user_id, seq, sold_at, kind, "
            "                 total_cents, tendered_cents, change_cents, refunds_sale_id, "
            "                 authorized_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (sale_id, register_id, shift_id, user_id, seq, ts, kind, total,
             tendered_cents, change, refunds_sale_id, authorized_by))
        for r in resolved:
            con.execute(
                "INSERT INTO sale_line(sale_id, product_id, name_at_sale, unit_price_cents, "
                "                      qty, line_total_cents) VALUES(?,?,?,?,?,?)",
                (sale_id, r["product_id"], r["name_at_sale"], r["unit_price_cents"],
                 r["qty"], r["line_total_cents"]))

        payload = {"id": sale_id, "register_id": register_id, "shift_id": shift_id,
                   "user_id": user_id, "seq": seq, "sold_at": ts, "kind": kind,
                   "total_cents": total, "tendered_cents": tendered_cents,
                   "change_cents": change, "refunds_sale_id": refunds_sale_id,
                   "lines": resolved}
        _outbox(con, "sale", sale_id, payload)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    return {"id": sale_id, "seq": seq, "total_cents": total,
            "tendered_cents": tendered_cents, "change_cents": tendered_cents - total,
            "sold_at": ts, "lines": resolved}


# --------------------------------------------------------- cash movements

def cash_movement(con, *, shift_id, kind, amount_cents, by_user,
                  authorized_by=None, envelope_no=None, note=None) -> dict:
    mid, ts = str(uuid.uuid4()), now_iso()
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute(
            "INSERT INTO cash_movement(id, register_id, shift_id, kind, amount_cents, "
            "  envelope_no, by_user, authorized_by, at, note) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (mid, meta(con, "register_id"), shift_id, kind, amount_cents,
             envelope_no, by_user, authorized_by, ts, note))
        _outbox(con, "cash_movement", mid,
                {"id": mid, "shift_id": shift_id, "kind": kind,
                 "amount_cents": amount_cents, "envelope_no": envelope_no,
                 "by_user": by_user, "authorized_by": authorized_by, "at": ts})
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return {"id": mid, "at": ts, "amount_cents": amount_cents, "envelope_no": envelope_no}


def next_envelope_no(con, shift_id: str) -> int:
    row = con.execute("SELECT COALESCE(MAX(envelope_no), 0) + 1 AS n FROM cash_movement "
                      "WHERE kind = 'drop'").fetchone()
    return int(row["n"])


def outbox_pending(con) -> int:
    return con.execute("SELECT COUNT(*) AS n FROM sync_outbox WHERE sent_at IS NULL"
                       ).fetchone()["n"]
