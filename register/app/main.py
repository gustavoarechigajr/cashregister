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


app.mount("/static", StaticFiles(directory=STATIC), name="static")
