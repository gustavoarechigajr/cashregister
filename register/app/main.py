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

from . import db, money

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

app = FastAPI(title="Cash Register", docs_url=None, redoc_url=None)

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
        }


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
        rows = c.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(total_cents),0) t FROM sale "
            "WHERE shift_id = ? AND kind = 'sale'", (shift["id"],)).fetchone()
        drops = c.execute(
            "SELECT id, at, amount_cents, envelope_no FROM cash_movement "
            "WHERE shift_id = ? AND kind = 'drop' ORDER BY at", (shift["id"],)).fetchall()
        return {
            "shift_id": shift["id"], "opened_at": shift["opened_at"],
            "opening_float_cents": shift["opening_float_cents"],
            "sales_count": rows["n"], "sales_cents": rows["t"],
            "drops": [dict(d) for d in drops],
            "expected_cents": db.shift_expected_cents(c, shift["id"]),
        }


app.mount("/static", StaticFiles(directory=STATIC), name="static")
