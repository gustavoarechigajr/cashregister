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

import hashlib
import hmac
import json
import os
import secrets
import time

import psycopg2
import psycopg2.extras
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import barcode as bc

DSN = os.environ.get("CAJA_DSN", "dbname=caja user=caja host=/var/run/postgresql")
TOKEN = os.environ.get("CAJA_SYNC_TOKEN", "")

app = FastAPI(title="caja central", docs_url=None, redoc_url=None)

STATIC = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


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
    con = psycopg2.connect(DSN)
    # Pinned, not inherited. systemd starts this service with no locale, so
    # libpq negotiates SQL_ASCII and every product with an n-tilde or an
    # accent fails to encode -- which is most of a Spanish catalogue.
    con.set_client_encoding("UTF8")
    return con


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


# -------------------------------------------------------------------- auth
# A single shared admin password, set in /etc/caja/env. Deliberately modest:
# this is a LAN service for two admins, not a multi-tenant app, and inventing
# per-user accounts here would duplicate the till's user table for no gain.
#
# What it is NOT is optional. The UI now edits the catalogue and holds four
# years of sales; leaving that open to every device on the VLAN was acceptable
# for a read-only prototype and is not acceptable now.

PASSWORD = os.environ.get("CAJA_ADMIN_PASSWORD", "")
SESSION_HOURS = 12
_sessions: dict[str, float] = {}          # token -> expiry (unix)


def _reap():
    now = time.time()
    for t, exp in list(_sessions.items()):
        if exp < now:
            _sessions.pop(t, None)


class LoginIn(BaseModel):
    password: str


@app.post("/api/login")
def login(body: LoginIn, response: Response):
    if not PASSWORD:
        raise HTTPException(503, "no_password_configured")
    # compare_digest so a wrong password takes the same time as a right one.
    if not hmac.compare_digest(body.password, PASSWORD):
        raise HTTPException(401, "bad_password")
    _reap()
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + SESSION_HOURS * 3600
    response.set_cookie("caja_sid", token, httponly=True, samesite="strict",
                        max_age=SESSION_HOURS * 3600)
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response, caja_sid: str | None = Cookie(default=None)):
    _sessions.pop(caja_sid or "", None)
    response.delete_cookie("caja_sid")
    return {"ok": True}


def require_ui(caja_sid: str | None = Cookie(default=None)):
    """Gate for everything the browser calls. /api/sync uses its own token."""
    if not PASSWORD:                       # unconfigured: fail closed, not open
        raise HTTPException(503, "no_password_configured")
    _reap()
    if not caja_sid or caja_sid not in _sessions:
        raise HTTPException(401, "no_session")
    return caja_sid


@app.get("/api/session")
def whoami(caja_sid: str | None = Cookie(default=None)):
    _reap()
    return {"authenticated": bool(caja_sid and caja_sid in _sessions),
            "configured": bool(PASSWORD)}


# ------------------------------------------------------------------ reports
# Read-only views over what the registers have sent. Product names come from
# the sale_line snapshots rather than a product table: the catalogue push does
# not exist yet, so `product` is empty, and reporting must work from what has
# actually arrived rather than what we wish had.

def _rows(sql, args=()):
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, args)
        return cur.fetchall()


@app.get("/api/report/summary")
def report_summary(_=Depends(require_ui)):
    totals = _rows("""
        SELECT
          -- Local day, not UTC. At 19:00 in Mexico it is already tomorrow in
          -- UTC, so date_trunc('day', now()) reported "today" as empty for the
          -- five hours of trading that matter most.
          COUNT(*) FILTER (WHERE (sold_at AT TIME ZONE 'America/Mexico_City')::date
                                 = (now() AT TIME ZONE 'America/Mexico_City')::date) AS tickets_today,
          COALESCE(SUM(total_cents) FILTER (WHERE (sold_at AT TIME ZONE 'America/Mexico_City')::date
                                 = (now() AT TIME ZONE 'America/Mexico_City')::date), 0) AS cents_today,
          COUNT(*) FILTER (WHERE sold_at >= now() - interval '7 days')  AS tickets_7d,
          COALESCE(SUM(total_cents) FILTER (WHERE sold_at >= now() - interval '7 days'), 0) AS cents_7d,
          COUNT(*) AS tickets_all,
          COALESCE(SUM(total_cents), 0) AS cents_all
        FROM sale WHERE kind = 'sale'""")[0]
    regs = _rows("""
        SELECT r.id, r.name, r.last_seen,
               -- last_seen is the heartbeat (the till checked in, with or
               -- without data); last_sync is the last batch that carried rows.
               -- Showing only the latter made a healthy idle till look dead.
               GREATEST(r.last_seen,
                        COALESCE((SELECT max(received_at) FROM sync_batch b
                                   WHERE b.register_id = r.id), r.last_seen)) AS last_sync,
               (SELECT max(received_at) FROM sync_batch b WHERE b.register_id = r.id) AS last_batch,
               (SELECT count(*) FROM sale s WHERE s.register_id = r.id) AS sales
        FROM register r ORDER BY r.name NULLS LAST""")
    return {"totals": totals, "registers": regs}


@app.get("/api/report/by_day")
def report_by_day(days: int = 30, _=Depends(require_ui)):
    return {"days": _rows("""
        SELECT (sold_at AT TIME ZONE 'America/Mexico_City')::date AS day,
               COUNT(*) AS tickets, SUM(total_cents) AS cents
        FROM sale WHERE kind = 'sale' AND sold_at >= now() - (%s || ' days')::interval
        GROUP BY 1 ORDER BY 1 DESC""", (days,))}


@app.get("/api/report/sales")
def report_sales(limit: int = 50, _=Depends(require_ui)):
    sales = _rows("""
        SELECT id, seq, sold_at, total_cents, tendered_cents, change_cents, kind, user_id
        FROM sale ORDER BY sold_at DESC, seq DESC LIMIT %s""", (limit,))
    if sales:
        lines = _rows("""
            SELECT sale_id, name_at_sale, qty, unit_price_cents, line_total_cents
            FROM sale_line WHERE sale_id = ANY(%s::uuid[]) ORDER BY sale_id, line_no""",
            ([str(s["id"]) for s in sales],))
        by = {}
        for l in lines:
            by.setdefault(str(l["sale_id"]), []).append(l)
        for s in sales:
            s["lines"] = by.get(str(s["id"]), [])
    return {"sales": sales}


@app.get("/api/report/shifts")
def report_shifts(limit: int = 30, _=Depends(require_ui)):
    return {"shifts": _rows("""
        SELECT s.id, s.opened_at, s.closed_at, s.opening_float_cents, s.counted_cents,
               s.expected_cents, s.difference_cents, s.user_id,
               (SELECT count(*) FROM sale x WHERE x.shift_id = s.id AND x.kind='sale') AS tickets,
               (SELECT COALESCE(SUM(total_cents),0) FROM sale x
                 WHERE x.shift_id = s.id AND x.kind='sale') AS sales_cents,
               (SELECT COALESCE(SUM(amount_cents),0) FROM cash_movement m
                 WHERE m.shift_id = s.id AND m.kind='drop') AS drops_cents
        FROM shift s ORDER BY COALESCE(s.opened_at, s.closed_at) DESC NULLS LAST LIMIT %s""",
        (limit,))}


@app.get("/api/report/products")
def report_products(limit: int = 50, _=Depends(require_ui)):
    # DISTINCT ON gives the most recent name each product was sold under, so a
    # renamed product does not split into two rows in the ranking.
    return {"products": _rows("""
        WITH latest AS (
          SELECT DISTINCT ON (sl.product_id) sl.product_id, sl.name_at_sale
          FROM sale_line sl JOIN sale s ON s.id = sl.sale_id
          ORDER BY sl.product_id, s.sold_at DESC
        )
        SELECT sl.product_id, l.name_at_sale AS name,
               SUM(sl.qty) AS qty, SUM(sl.line_total_cents) AS cents
        FROM sale_line sl
        JOIN sale s ON s.id = sl.sale_id AND s.kind = 'sale'
        JOIN latest l ON l.product_id = sl.product_id
        GROUP BY sl.product_id, l.name_at_sale
        ORDER BY qty DESC LIMIT %s""", (limit,))}


# --------------------------------------------------------------- catalogue
# Central now holds the catalogue, seeded from the till (tools/seed_catalogue.py).
# NOTE: editing here does not yet reach the register -- the push-down is not
# built. Until it is, the till remains the effective master and these writes
# are for planning and reporting. The UI says so plainly rather than pretending.

def _exec(sql, args=(), fetch=False):
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, args)
        return cur.fetchall() if fetch else cur.rowcount


def _bump_catalogue():
    """
    Advance the catalogue revision. Every write a register would care about
    must call this, or the till keeps serving the old price.
    """
    _exec("INSERT INTO meta (key, value) VALUES ('catalogue_revision','1') "
          "ON CONFLICT (key) DO UPDATE SET value = (meta.value::bigint + 1)::text")


def _catalogue_revision() -> int:
    r = _rows("SELECT value FROM meta WHERE key='catalogue_revision'")
    return int(r[0]["value"]) if r else 0


@app.get("/api/catalogue/pull")
def catalogue_pull(since: int = -1, authorization: str | None = Header(default=None)):
    """
    What the register pulls. Authenticated with the SYNC token, not a cookie:
    the caller is a daemon, not a browser.

    Returns the FULL catalogue, not a delta. At ~200 products that is a few
    tens of kilobytes, and a full snapshot is immune to a missed increment --
    a delta scheme would require the register to have observed every
    intermediate revision, which an offline-first till cannot promise.
    `since` is an optimisation only: a matching revision returns no payload.

    Barcodes ARE included. Generation moved here once it became clear the label
    sheet is printed on an ordinary printer and there is no ordinary printer in
    the store -- the admin prints from this console, so central mints the codes
    and the till receives them.
    """
    if TOKEN and authorization != "Bearer " + TOKEN:
        raise HTTPException(401, "bad_token")
    rev = _catalogue_revision()
    if since == rev:
        return {"revision": rev, "changed": False}
    return {
        "revision": rev, "changed": True,
        "categories": _rows("SELECT id, name, sort_order FROM category ORDER BY sort_order, name"),
        "products": _rows(
            "SELECT id, category_id, name, price_cents, cost_cents, is_active "
            "FROM product ORDER BY id"),
        "barcodes": _rows(
            "SELECT code, product_id, is_internal FROM barcode ORDER BY code"),
    }


class ProductIn(BaseModel):
    name: str
    category_id: str
    price_cents: int
    cost_cents: int | None = None
    is_active: bool = True
    reorder_level: int | None = None


@app.get("/api/catalogue")
def catalogue(q: str = "", category: str = "", inactive: bool = False,
              _=Depends(require_ui)):
    where, args = ["true"], []
    if q:
        where.append("(p.name ILIKE %s OR EXISTS (SELECT 1 FROM barcode b"
                     "  WHERE b.product_id = p.id AND b.code ILIKE %s))")
        args += ["%" + q + "%", q + "%"]
    if category:
        where.append("p.category_id = %s"); args.append(category)
    if not inactive:
        where.append("p.is_active")
    rows = _rows("""
        SELECT p.id, p.name, p.category_id, c.name AS category_name, p.price_cents,
               p.cost_cents, p.is_active, p.reorder_level,
               COALESCE((SELECT array_agg(b.code ORDER BY b.code)
                           FROM barcode b WHERE b.product_id = p.id), '{}') AS barcodes,
               COALESCE((SELECT SUM(sl.qty) FROM sale_line sl JOIN sale s ON s.id = sl.sale_id
                          WHERE sl.product_id = p.id AND s.kind = 'sale'), 0) AS sold,
               COALESCE((SELECT SUM(r.qty) FROM receiving r WHERE r.product_id = p.id), 0) AS received
        FROM product p LEFT JOIN category c ON c.id = p.category_id
        WHERE """ + " AND ".join(where) + " ORDER BY p.name", args)
    for r in rows:
        r["on_hand"] = int(r["received"]) - int(r["sold"])
    return {"products": rows}


@app.get("/api/categories")
def categories(_=Depends(require_ui)):
    return {"categories": _rows(
        "SELECT c.id, c.name, c.sort_order,"
        "       (SELECT count(*) FROM product p WHERE p.category_id = c.id) AS products "
        "FROM category c ORDER BY c.sort_order, c.name")}


@app.post("/api/catalogue/products")
def create_product(body: ProductIn, _=Depends(require_ui)):
    # ids are shared with the register, so a new one must not collide with a
    # product the till already has. Taking max+1 across the whole table is
    # crude but correct while the till is still creating ids of its own.
    row = _rows("INSERT INTO product (id, name, category_id, price_cents, cost_cents,"
                "                     is_active, reorder_level) "
                "VALUES ((SELECT COALESCE(MAX(id),0)+1 FROM product), %s,%s,%s,%s,%s,%s) "
                "RETURNING id", (body.name.strip(), body.category_id, body.price_cents,
                                 body.cost_cents, body.is_active, body.reorder_level))
    _bump_catalogue()
    return {"id": row[0]["id"]}


@app.put("/api/catalogue/products/{pid}")
def update_product(pid: int, body: ProductIn, _=Depends(require_ui)):
    n = _exec("UPDATE product SET name=%s, category_id=%s, price_cents=%s, cost_cents=%s,"
              "  is_active=%s, reorder_level=%s, updated_at=now() WHERE id=%s",
              (body.name.strip(), body.category_id, body.price_cents, body.cost_cents,
               body.is_active, body.reorder_level, pid))
    if not n:
        raise HTTPException(404, "unknown_product")
    _bump_catalogue()
    return {"ok": True}


# ---------------------------------------------------------------- barcodes
# Generation lives here rather than on the till because the label sheet is
# printed on an ordinary printer, and there is no ordinary printer in the
# store. Codes push down to the register with the catalogue so scanning works.

class BarcodeIn(BaseModel):
    code: str


@app.get("/api/labels")
def labels(_=Depends(require_ui)):
    """Everything the label screen needs: what is missing a code, and what has one."""
    return {
        "missing": _rows(
            "SELECT p.id, p.name, p.price_cents FROM product p "
            "WHERE p.is_active AND NOT EXISTS "
            "  (SELECT 1 FROM barcode b WHERE b.product_id = p.id) ORDER BY p.name"),
        "internal": _rows(
            "SELECT b.code, p.id, p.name, p.price_cents FROM barcode b "
            "JOIN product p ON p.id = b.product_id "
            "WHERE b.is_internal ORDER BY p.name"),
    }


@app.post("/api/catalogue/products/{pid}/generate_barcode")
def generate_barcode(pid: int, _=Depends(require_ui)):
    if not _rows("SELECT 1 FROM product WHERE id=%s", (pid,)):
        raise HTTPException(404, "unknown_product")
    existing = [r["code"] for r in _rows("SELECT code FROM barcode")]
    code = bc.make_internal(bc.next_sequence(existing))
    _exec("INSERT INTO barcode (code, product_id, is_internal) VALUES (%s,%s,true)",
          (code, pid))
    _bump_catalogue()
    return {"code": code}


@app.post("/api/catalogue/products/{pid}/barcode")
def add_barcode(pid: int, body: BarcodeIn, _=Depends(require_ui)):
    code = bc.normalise(body.code)
    if not code:
        raise HTTPException(400, "empty_code")
    if bc.is_valid(code) is False:
        # Warn rather than refuse would be worse: a mistyped digit produces a
        # code that scans as nothing, and the mistake is only discovered at the
        # till with a customer waiting.
        raise HTTPException(400, "bad_check_digit")
    owner = _rows("SELECT product_id FROM barcode WHERE code=%s", (code,))
    if owner:
        if owner[0]["product_id"] != pid:
            raise HTTPException(409, "barcode_in_use")
        return {"code": code}
    if not _rows("SELECT 1 FROM product WHERE id=%s", (pid,)):
        raise HTTPException(404, "unknown_product")
    _exec("INSERT INTO barcode (code, product_id, is_internal) VALUES (%s,%s,%s)",
          (code, pid, bc.is_internal(code)))
    _bump_catalogue()
    return {"code": code}


class AdoptIn(BaseModel):
    barcodes: list[dict]


@app.post("/api/catalogue/barcodes/adopt")
def adopt_barcodes(body: AdoptIn, authorization: str | None = Header(default=None)):
    """
    Take on codes a register has that central does not.

    Sync-token auth: the caller is the till's daemon. Central owns *generation*
    now, but a code can still appear only on the register -- restored from a
    backup, or created before central took over -- and a code that scans at the
    till but is invisible in the console is a reporting hole.

    Insert-only, never update or delete. If central already knows the code it
    is left exactly as it is, because central is the authority on which product
    a code belongs to; this endpoint exists to fill gaps, not to let a till
    repoint a code.
    """
    if TOKEN and authorization != "Bearer " + TOKEN:
        raise HTTPException(401, "bad_token")
    adopted, skipped = 0, 0
    for b in body.barcodes[:500]:
        code = bc.normalise(str(b.get("code") or ""))
        pid = b.get("product_id")
        if not code or pid is None:
            skipped += 1
            continue
        # A code whose product central has never heard of would violate the FK.
        # Skip rather than fail the batch: one odd row must not block the rest.
        if not _rows("SELECT 1 FROM product WHERE id=%s", (pid,)):
            skipped += 1
            continue
        n = _exec("INSERT INTO barcode (code, product_id, is_internal) VALUES (%s,%s,%s) "
                  "ON CONFLICT (code) DO NOTHING",
                  (code, pid, bool(b.get("is_internal")) or bc.is_internal(code)))
        adopted += n
    if adopted:
        _bump_catalogue()
    return {"adopted": adopted, "skipped": skipped}


@app.delete("/api/catalogue/barcodes/{code}")
def delete_barcode(code: str, _=Depends(require_ui)):
    if not _exec("DELETE FROM barcode WHERE code=%s", (bc.normalise(code),)):
        raise HTTPException(404, "unknown_code")
    _bump_catalogue()
    return {"ok": True}


# --------------------------------------------------------------- inventory

class ReceivingIn(BaseModel):
    product_id: int
    qty: int                      # negative is allowed: shrinkage, breakage, a miscount
    unit_cost_cents: int | None = None
    note: str | None = None


@app.post("/api/receiving")
def add_receiving(body: ReceivingIn, _=Depends(require_ui)):
    """
    Record stock arriving (or leaving for a reason that is not a sale).

    Negative quantities are deliberately allowed. On-hand is derived as
    received - sold, so the only way to correct a miscount, a breakage or
    shrinkage is a compensating row. Editing history would be the alternative,
    and this project does not edit history anywhere else either.
    """
    if body.qty == 0:
        raise HTTPException(400, "qty_zero")
    if not _rows("SELECT 1 FROM product WHERE id=%s", (body.product_id,)):
        raise HTTPException(404, "unknown_product")
    _exec("INSERT INTO receiving (product_id, qty, unit_cost_cents, note) VALUES (%s,%s,%s,%s)",
          (body.product_id, body.qty, body.unit_cost_cents, (body.note or "").strip() or None))
    return {"ok": True}


@app.get("/api/stock")
def stock(low_only: bool = False, _=Depends(require_ui)):
    rows = _rows("""
        SELECT p.id, p.name, p.category_id, c.name AS category_name, p.reorder_level,
               p.price_cents, p.is_active,
               COALESCE((SELECT SUM(r.qty) FROM receiving r WHERE r.product_id=p.id),0) AS received,
               COALESCE((SELECT SUM(sl.qty) FROM sale_line sl JOIN sale s ON s.id=sl.sale_id
                          WHERE sl.product_id=p.id AND s.kind='sale'),0) AS sold
        FROM product p LEFT JOIN category c ON c.id=p.category_id
        WHERE p.is_active ORDER BY p.name""")
    out = []
    for r in rows:
        r["on_hand"] = int(r["received"]) - int(r["sold"])
        # Untracked (reorder_level NULL) is not "fine", it is "unknown", and the
        # UI shows it that way rather than as a healthy green row.
        if r["reorder_level"] is None:
            r["state"] = "untracked"
        elif r["on_hand"] <= 0:
            r["state"] = "out"
        elif r["on_hand"] <= r["reorder_level"]:
            r["state"] = "low"
        else:
            r["state"] = "ok"
        if not low_only or r["state"] in ("low", "out"):
            out.append(r)
    return {"stock": out}


@app.get("/api/receiving")
def list_receiving(limit: int = 100, _=Depends(require_ui)):
    return {"receiving": _rows(
        "SELECT r.id, r.product_id, p.name, r.qty, r.unit_cost_cents, r.received_at, r.note "
        "FROM receiving r LEFT JOIN product p ON p.id = r.product_id "
        "ORDER BY r.received_at DESC LIMIT %s", (limit,))}


# ----------------------------------------------------------- range reports

@app.get("/api/report/range")
def report_range(start: str, end: str, _=Depends(require_ui)):
    """Everything a printed report needs for a date range, in one round trip."""
    args = (start, end)
    win = ("(s.sold_at AT TIME ZONE 'America/Mexico_City')::date BETWEEN %s AND %s")
    totals = _rows(
        "SELECT COUNT(*) AS tickets, COALESCE(SUM(s.total_cents),0) AS cents,"
        "       COALESCE(AVG(s.total_cents),0)::bigint AS avg_cents "
        "FROM sale s WHERE s.kind='sale' AND " + win, args)[0]
    return {
        "start": start, "end": end, "totals": totals,
        "by_day": _rows(
            "SELECT (s.sold_at AT TIME ZONE 'America/Mexico_City')::date AS day,"
            "       COUNT(*) AS tickets, SUM(s.total_cents) AS cents "
            "FROM sale s WHERE s.kind='sale' AND " + win + " GROUP BY 1 ORDER BY 1", args),
        "by_product": _rows(
            "SELECT sl.name_at_sale AS name, SUM(sl.qty) AS qty, SUM(sl.line_total_cents) AS cents "
            "FROM sale_line sl JOIN sale s ON s.id=sl.sale_id "
            "WHERE s.kind='sale' AND " + win + " GROUP BY 1 ORDER BY qty DESC", args),
        "by_category": _rows(
            "SELECT COALESCE(c.name,'Sin categoría') AS name, SUM(sl.qty) AS qty,"
            "       SUM(sl.line_total_cents) AS cents "
            "FROM sale_line sl JOIN sale s ON s.id=sl.sale_id "
            "LEFT JOIN product p ON p.id=sl.product_id "
            "LEFT JOIN category c ON c.id=p.category_id "
            "WHERE s.kind='sale' AND " + win + " GROUP BY 1 ORDER BY cents DESC", args),
        "cash": _rows(
            "SELECT kind, COUNT(*) AS n, SUM(amount_cents) AS cents FROM cash_movement m "
            "WHERE (m.at AT TIME ZONE 'America/Mexico_City')::date BETWEEN %s AND %s "
            "GROUP BY kind", args),
    }


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
        # A heartbeat. The till posts even with nothing queued so that central
        # can tell "idle" from "gone" -- otherwise a register that simply has
        # no sales looks identical to one that has stopped talking.
        with _conn() as c, c.cursor() as cur:
            _seen_register(cur, batch.register_id, batch.register_name)
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
