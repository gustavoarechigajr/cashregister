"""
Central service — sync ingest.

The register drains its append-only `sync_outbox` here. Everything about this
endpoint is shaped by one requirement: **it must be safe to send the same rows
twice.** A link that drops mid-drain is the normal case, not the exception, and
the register cannot know whether a batch it never got a reply for was applied.
So every write is idempotent on the id the register generated, and the response
tells it the highest outbox id that is now durable.

Deliberately NOT enforced here:

  * Shifts arriving after their sales. Outbox order is preserved per register,
    but a partial drain can still land a sale whose shift row has not been
    inserted -- `sale.shift_id` is not a foreign key precisely so that a sale is
    never rejected for it.

  * Unknown users or products. Central does not own the till's user table, and
    products may retire. Rejecting on those would mean losing money data over
    bookkeeping.
"""

import json
import os

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

DSN = os.environ.get("CAJA_DSN", "dbname=caja user=caja host=/var/run/postgresql")
TOKEN = os.environ.get("CAJA_SYNC_TOKEN", "")

app = FastAPI(title="caja central")


class Row(BaseModel):
    id: int                 # the register's sync_outbox.id -- its resume cursor
    entity: str
    entity_id: str
    payload: dict
    created_at: str


class Batch(BaseModel):
    register_id: str
    register_name: str | None = None
    rows: list[Row]


def _conn():
    return psycopg2.connect(DSN)


def _seen_register(cur, register_id, name):
    cur.execute(
        "INSERT INTO register(id, name) VALUES (%s, %s) "
        "ON CONFLICT (id) DO UPDATE SET last_seen = now(), "
        "  name = COALESCE(EXCLUDED.name, register.name)",
        (register_id, name))


def _apply_shift(cur, reg, p):
    """
    Shifts are emitted twice: once on open, once on close. The close payload
    carries only the closing fields, so it must not blank the opening ones --
    hence COALESCE on every column rather than a plain overwrite.
    """
    cur.execute(
        "INSERT INTO shift (id, register_id, user_id, opened_at, opening_float_cents,"
        "                   closed_at, counted_cents, expected_cents, difference_cents,"
        "                   closed_by, authorized_by) "
        "VALUES (%(id)s, %(reg)s, %(user_id)s, %(opened_at)s, %(float)s,"
        "        %(closed_at)s, %(counted)s, %(expected)s, %(diff)s, %(closed_by)s, %(auth)s) "
        "ON CONFLICT (id) DO UPDATE SET "
        "  user_id             = COALESCE(shift.user_id, EXCLUDED.user_id),"
        "  opened_at           = COALESCE(shift.opened_at, EXCLUDED.opened_at),"
        "  opening_float_cents = GREATEST(shift.opening_float_cents, EXCLUDED.opening_float_cents),"
        "  closed_at           = COALESCE(EXCLUDED.closed_at, shift.closed_at),"
        "  counted_cents       = COALESCE(EXCLUDED.counted_cents, shift.counted_cents),"
        "  expected_cents      = COALESCE(EXCLUDED.expected_cents, shift.expected_cents),"
        "  difference_cents    = COALESCE(EXCLUDED.difference_cents, shift.difference_cents),"
        "  closed_by           = COALESCE(EXCLUDED.closed_by, shift.closed_by),"
        "  authorized_by       = COALESCE(EXCLUDED.authorized_by, shift.authorized_by)",
        {"id": p["id"], "reg": reg, "user_id": p.get("user_id"),
         "opened_at": p.get("opened_at"), "float": p.get("opening_float_cents") or 0,
         "closed_at": p.get("closed_at"), "counted": p.get("counted_cents"),
         "expected": p.get("expected_cents"), "diff": p.get("difference_cents"),
         "closed_by": p.get("closed_by"), "auth": p.get("authorized_by")})


def _apply_sale(cur, reg, p):
    cur.execute(
        "INSERT INTO sale (id, register_id, shift_id, user_id, seq, sold_at, kind,"
        "                  total_cents, tendered_cents, change_cents, refunds_sale_id,"
        "                  authorized_by) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
        (p["id"], reg, p.get("shift_id"), p.get("user_id"), p["seq"], p["sold_at"],
         p.get("kind", "sale"), p["total_cents"], p.get("tendered_cents", 0),
         p.get("change_cents", 0), p.get("refunds_sale_id"), p.get("authorized_by")))
    # Lines are written only alongside a first insert of their sale. On a
    # replay the sale conflicts, and re-inserting lines would double the
    # quantities that v_stock_on_hand derives -- the exact bug idempotency
    # exists to prevent.
    if cur.rowcount == 0:
        return
    for n, l in enumerate(p.get("lines", []), start=1):
        cur.execute(
            "INSERT INTO sale_line (sale_id, line_no, product_id, name_at_sale,"
            "                       unit_price_cents, qty, line_total_cents) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (p["id"], n, l["product_id"], l["name_at_sale"],
             l["unit_price_cents"], l["qty"], l["line_total_cents"]))


def _apply_cash(cur, reg, p):
    cur.execute(
        "INSERT INTO cash_movement (id, register_id, shift_id, kind, amount_cents,"
        "                           envelope_no, by_user, authorized_by, at, note) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
        (p["id"], reg, p.get("shift_id"), p["kind"], p["amount_cents"],
         p.get("envelope_no"), p.get("by_user"), p.get("authorized_by"),
         p["at"], p.get("note")))


def _apply_audit(cur, reg, p):
    cur.execute(
        "INSERT INTO audit_event (id, register_id, at, action, by_user, authorized_by, detail) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
        (p["id"], reg, p["at"], p["action"], p.get("by_user"),
         p.get("authorized_by"), json.dumps(p.get("detail") or {})))


HANDLERS = {"shift": _apply_shift, "sale": _apply_sale,
            "cash_movement": _apply_cash, "audit_event": _apply_audit}


@app.get("/api/health")
def health():
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT 1")
        return {"ok": True}


@app.post("/api/sync")
def sync(batch: Batch, authorization: str | None = Header(default=None)):
    if TOKEN and authorization != "Bearer " + TOKEN:
        raise HTTPException(401, "bad_token")
    if not batch.rows:
        return {"applied": 0, "max_outbox_id": None}

    applied = 0
    # One transaction for the whole batch: either the register can advance its
    # cursor past all of these, or it retries all of them. A partially applied
    # batch with an advanced cursor is how sales go missing.
    with _conn() as c:
        with c.cursor() as cur:
            _seen_register(cur, batch.register_id, batch.register_name)
            for row in sorted(batch.rows, key=lambda r: r.id):
                fn = HANDLERS.get(row.entity)
                if fn is None:
                    raise HTTPException(400, "unknown_entity: " + row.entity)
                fn(cur, batch.register_id, row.payload)
                applied += 1
            max_id = max(r.id for r in batch.rows)
            cur.execute(
                "INSERT INTO sync_batch (register_id, rows_sent, rows_applied, max_outbox_id) "
                "VALUES (%s,%s,%s,%s)",
                (batch.register_id, len(batch.rows), applied, max_id))
    return {"applied": applied, "max_outbox_id": max_id}
