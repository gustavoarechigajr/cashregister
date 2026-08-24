"""
Register HTTP service.

Binds to 127.0.0.1 only — the till UI is served to the kiosk browser on the same
machine and nothing else should be able to reach it. All money arithmetic happens
here, never in the browser: the client sends product ids and quantities, the
server resolves prices from the catalogue and computes the total.
"""

import os
import secrets
from contextlib import contextmanager

from fastapi import Cookie, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import barcode as bc
from . import db, devices, money

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

app = FastAPI(title="Cash Register", docs_url=None, redoc_url=None)


@app.middleware("http")
async def no_cache(request, call_next):
    # This is a kiosk: one Chromium instance, redeployed over SSH, restarted
    # in place. Its on-disk HTTP cache survives a kiosk restart (a fresh
    # process navigating to the same URL, not a hard reload), so without
    # this a stale index.html can pair with a freshly-fetched app.js that
    # references elements the cached HTML doesn't have yet -- a real
    # incident, not a hypothetical: it silently blanked the till after a
    # deploy that added new overlay markup. FileResponse/StaticFiles set
    # Last-Modified/ETag but no Cache-Control, which leaves the browser free
    # to skip revalidation entirely under heuristic freshness rules.
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response

# Single-till kiosk: sessions live in memory. A restart asks the cashier to
# re-enter a 4-digit PIN, which is the correct outcome — not a reason for
# persistent tokens on a machine that handles cash.
_sessions: dict[str, dict] = {}


@contextmanager
def conn():
    c = db.connect()
    try:
        yield c
    finally:
        c.close()


def require_session(token: str | None) -> dict:
    s = _sessions.get(token or "")
    if not s:
        raise HTTPException(401, "no session")
    return s


def require_admin(c, pin: str) -> dict:
    """An override: any admin's PIN authorises, and both people end up on the record."""
    for u in db.users(c):
        if u["role"] != "admin":
            continue
        who, err = db.authenticate(c, u["id"], pin)
        if who:
            return who
    raise HTTPException(403, "override_denied")


def require_admin_session(sid: str | None) -> dict:
    """
    Gates the admin pages themselves: is the person CURRENTLY LOGGED IN on
    this till an admin? Distinct from require_admin() above, which checks a
    freshly-typed PIN against any admin for a one-off override -- this checks
    the standing session, the way require_session() does for the till.
    """
    s = require_session(sid)
    if s["role"] != "admin":
        raise HTTPException(403, "admin_only")
    return s


# ------------------------------------------------------------------ models

class LoginIn(BaseModel):
    user_id: int
    pin: str = Field(min_length=4, max_length=12)


class OpenShiftIn(BaseModel):
    opening_float_cents: int = Field(ge=0)


class SaleLineIn(BaseModel):
    product_id: int
    qty: int = Field(gt=0, le=999)


class SaleIn(BaseModel):
    lines: list[SaleLineIn]
    tendered_cents: int = Field(ge=0)


class DropIn(BaseModel):
    amount_cents: int = Field(gt=0)
    admin_pin: str


class CloseShiftIn(BaseModel):
    counted_cents: int = Field(ge=0)
    admin_pin: str | None = None


class VerifyPinIn(BaseModel):
    pin: str


# A shortfall beyond this needs an admin's PIN to close the shift. An overage
# never blocks closing -- only cash actually missing is a control problem.
SHORTFALL_REQUIRES_ADMIN_CENTS = -5000


# ------------------------------------------------------------------ routes

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/api/bootstrap")
def bootstrap(sid: str | None = Cookie(default=None)):
    with conn() as c:
        shift = db.current_shift(c)
        session = _sessions.get(sid or "")
        return {
            "register_id": db.meta(c, "register_id"),
            "users": db.users(c),
            "session": session,
            "shift": (None if not shift else {
                "id": shift["id"],
                "opened_at": shift["opened_at"],
                "user_id": shift["user_id"],
                "opening_float_cents": shift["opening_float_cents"],
                "expected_cents": db.shift_expected_cents(c, shift["id"]),
            }),
            "catalogue": db.catalogue(c),
            "outbox_pending": db.outbox_pending(c),
            "devices": devices.status(),
        }


@app.get("/api/devices")
def device_status():
    """Polled by the header. Deliberately needs no session: an unattended till
    should still show that its printer has died."""
    return devices.status()


@app.post("/api/verify_admin")
def verify_admin(body: VerifyPinIn, sid: str | None = Cookie(default=None)):
    """
    Confirms a PIN belongs to an admin, with no side effect of its own.

    Exists so a guarded action with nothing else to call server-side (like
    cancelling a sale, which has no row to update before COBRAR is pressed)
    still has its PIN actually checked, rather than trusting any six digits
    the way the client-only override modal did before this endpoint existed.
    """
    require_session(sid)
    with conn() as c:
        admin = require_admin(c, body.pin)
        return {"id": admin["id"], "name": admin["name"]}


@app.post("/api/login")
def login(body: LoginIn, response: Response):
    with conn() as c:
        user, err = db.authenticate(c, body.user_id, body.pin)
        if err:
            raise HTTPException(401, err)
        token = secrets.token_urlsafe(24)
        _sessions[token] = user
        response.set_cookie("sid", token, httponly=True, samesite="strict", max_age=57600)
        return {"session": user}


@app.post("/api/logout")
def logout(response: Response, sid: str | None = Cookie(default=None)):
    _sessions.pop(sid or "", None)
    response.delete_cookie("sid")
    return {"ok": True}


@app.get("/api/scan")
def scan(code: str, sid: str | None = Cookie(default=None)):
    require_session(sid)
    with conn() as c:
        p = db.find_by_barcode(c, code)
        if not p:
            raise HTTPException(404, "unknown_barcode")
        return p


@app.post("/api/shift/open")
def shift_open(body: OpenShiftIn, sid: str | None = Cookie(default=None)):
    s = require_session(sid)
    with conn() as c:
        shift = db.open_shift(c, s["id"], body.opening_float_cents)
        return {"id": shift["id"], "opened_at": shift["opened_at"],
                "opening_float_cents": shift["opening_float_cents"]}


@app.post("/api/sale")
def sale(body: SaleIn, sid: str | None = Cookie(default=None)):
    s = require_session(sid)
    with conn() as c:
        shift = db.current_shift(c)
        if not shift:
            raise HTTPException(409, "no_open_shift")
        try:
            result = db.commit_sale(
                c, shift_id=shift["id"], user_id=s["id"],
                lines=[l.model_dump() for l in body.lines],
                tendered_cents=body.tendered_cents)
        except ValueError as e:
            raise HTTPException(400, str(e))
        result["total"] = money.format_mxn(result["total_cents"])
        result["change"] = money.format_mxn(result["change_cents"])
        return result


@app.post("/api/cash/drop")
def cash_drop(body: DropIn, sid: str | None = Cookie(default=None)):
    """Retiro parcial. Cash leaving the drawer always needs an admin on the record."""
    s = require_session(sid)
    with conn() as c:
        shift = db.current_shift(c)
        if not shift:
            raise HTTPException(409, "no_open_shift")
        admin = require_admin(c, body.admin_pin)
        env = db.next_envelope_no(c, shift["id"])
        mv = db.cash_movement(c, shift_id=shift["id"], kind="drop",
                              amount_cents=body.amount_cents, by_user=s["id"],
                              authorized_by=admin["id"], envelope_no=env)
        db.audit(c, "cash_drop", by_user=s["id"], authorized_by=admin["id"],
                 detail={"amount_cents": body.amount_cents, "envelope_no": env})
        return {**mv, "authorized_by": admin["name"],
                "expected_cents": db.shift_expected_cents(c, shift["id"])}


@app.get("/api/shift/summary")
def shift_summary(sid: str | None = Cookie(default=None)):
    require_session(sid)
    with conn() as c:
        shift = db.current_shift(c)
        if not shift:
            raise HTTPException(409, "no_open_shift")
        return db.shift_summary(c, shift["id"])


@app.post("/api/shift/close")
def shift_close(body: CloseShiftIn, sid: str | None = Cookie(default=None)):
    s = require_session(sid)
    with conn() as c:
        shift = db.current_shift(c)
        if not shift:
            raise HTTPException(409, "no_open_shift")
        expected = db.shift_expected_cents(c, shift["id"])
        diff = body.counted_cents - expected

        authorized_by = None
        if diff < SHORTFALL_REQUIRES_ADMIN_CENTS:
            if not body.admin_pin:
                raise HTTPException(403, "authorization_required")
            admin = require_admin(c, body.admin_pin)
            authorized_by = admin["id"]

        result = db.close_shift(c, shift_id=shift["id"], counted_cents=body.counted_cents,
                                closed_by=s["id"], authorized_by=authorized_by)
        db.audit(c, "shift_close", by_user=s["id"], authorized_by=authorized_by,
                 detail={"counted_cents": body.counted_cents, "expected_cents": expected,
                        "difference_cents": diff})
        return {**result,
                "counted": money.format_mxn(result["counted_cents"]),
                "expected": money.format_mxn(result["expected_cents"]),
                "difference": money.format_mxn(result["difference_cents"])}



# ------------------------------------------------------------------ admin models

class ProductCreateIn(BaseModel):
    category_id: str
    name: str = Field(min_length=1, max_length=200)
    price_cents: int = Field(ge=0)
    cost_cents: int | None = Field(default=None, ge=0)
    is_active: bool = True


class ProductUpdateIn(BaseModel):
    category_id: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    price_cents: int | None = Field(default=None, ge=0)
    cost_cents: int | None = -1          # -1 sentinel: field omitted == "leave unchanged"
    cost_cents_set: bool = False         # true if the client actually wants to change it
    is_active: bool | None = None


class BarcodeAddIn(BaseModel):
    code: str = Field(min_length=6, max_length=14)


# ------------------------------------------------------------------ admin routes

@app.get("/admin")
def admin_page(sid: str | None = Cookie(default=None)):
    require_admin_session(sid)
    return FileResponse(os.path.join(STATIC, "admin.html"))


@app.get("/api/admin/session")
def admin_session(sid: str | None = Cookie(default=None)):
    """So admin.html can confirm it's really allowed in, and show who's logged in."""
    s = require_admin_session(sid)
    return {"session": s}


@app.get("/api/admin/products")
def admin_products(q: str | None = None, missing_barcode: bool = False,
                   sid: str | None = Cookie(default=None)):
    require_admin_session(sid)
    with conn() as c:
        rows = db.list_products_admin(c, q=q, only_missing_barcode=missing_barcode)
        for r in rows:
            r["price"] = money.format_mxn(r["price_cents"])
            r["cost"] = money.format_mxn(r["cost_cents"]) if r["cost_cents"] is not None else None
        return {"products": rows, "categories": db.list_categories_all(c)}


@app.post("/api/admin/products")
def admin_create_product(body: ProductCreateIn, sid: str | None = Cookie(default=None)):
    require_admin_session(sid)
    with conn() as c:
        cats = {cat["id"] for cat in db.list_categories_all(c)}
        if body.category_id not in cats:
            raise HTTPException(400, "unknown_category")
        p = db.create_product(c, category_id=body.category_id, name=body.name.strip(),
                              price_cents=body.price_cents, cost_cents=body.cost_cents,
                              is_active=body.is_active)
        return p


@app.put("/api/admin/products/{product_id}")
def admin_update_product(product_id: int, body: ProductUpdateIn,
                         sid: str | None = Cookie(default=None)):
    require_admin_session(sid)
    with conn() as c:
        if body.category_id is not None:
            cats = {cat["id"] for cat in db.list_categories_all(c)}
            if body.category_id not in cats:
                raise HTTPException(400, "unknown_category")
        p = db.update_product(
            c, product_id,
            category_id=body.category_id,
            name=body.name.strip() if body.name is not None else None,
            price_cents=body.price_cents,
            cost_cents=(body.cost_cents if body.cost_cents_set else ...),
            is_active=body.is_active)
        if p is None:
            raise HTTPException(404, "unknown_product")
        return p


@app.post("/api/admin/products/{product_id}/barcode")
def admin_add_barcode(product_id: int, body: BarcodeAddIn,
                      sid: str | None = Cookie(default=None)):
    """Attach a barcode typed or scanned in by an admin -- a real supplier
    code found on packaging, as distinct from a generated internal one."""
    require_admin_session(sid)
    with conn() as c:
        if db.get_product_admin(c, product_id) is None:
            raise HTTPException(404, "unknown_product")
        norm = bc.normalise(body.code)
        owner = c.execute("SELECT product_id FROM barcode WHERE code = ?", (norm,)).fetchone()
        if owner is not None:
            raise HTTPException(409, "barcode_in_use")
        db.add_barcode(c, product_id, norm, is_internal=norm.startswith("2"),
                       printed=body.code if body.code != norm else None)
        return db.get_product_admin(c, product_id)


@app.post("/api/admin/products/{product_id}/generate_barcode")
def admin_generate_barcode(product_id: int, sid: str | None = Cookie(default=None)):
    require_admin_session(sid)
    with conn() as c:
        if db.get_product_admin(c, product_id) is None:
            raise HTTPException(404, "unknown_product")
        seq = db.next_internal_sequence(c)
        code = bc.make_internal(seq)
        db.add_barcode(c, product_id, code, is_internal=True)
        return db.get_product_admin(c, product_id)


@app.delete("/api/admin/barcodes/{code}")
def admin_delete_barcode(code: str, sid: str | None = Cookie(default=None)):
    require_admin_session(sid)
    with conn() as c:
        db.delete_barcode(c, code)
        return {"ok": True}

app.mount("/static", StaticFiles(directory=STATIC), name="static")
