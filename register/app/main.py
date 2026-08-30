"""
Register HTTP service.

Binds to 127.0.0.1 only — the till UI is served to the kiosk browser on the same
machine and nothing else should be able to reach it. All money arithmetic happens
here, never in the browser: the client sends product ids and quantities, the
server resolves prices from the catalogue and computes the total.
"""

import os
import secrets
import subprocess
import threading
import time
from contextlib import contextmanager

from fastapi import Cookie, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import barcode as bc
from . import db, devices, money, printer, sync

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


# How long an elevated cashier keeps admin access. This is an IDLE timeout --
# every admin request that passes the gate below pushes it out again -- so a
# long barcode session does not expire under someone mid-scan, while a pad left
# unattended closes itself. A PIN typed on a shop-floor kiosk is shoulder-
# surfable, so this is deliberately short rather than a whole shift.
ELEVATION_SECONDS = 180


def require_admin_session(sid: str | None) -> dict:
    """
    Gates the admin pages themselves: is the person CURRENTLY LOGGED IN on
    this till an admin? Distinct from require_admin() above, which checks a
    freshly-typed PIN against any admin for a one-off override -- this checks
    the standing session, the way require_session() does for the till.

    A cashier may also hold a temporary elevation granted by /api/admin/elevate
    (an admin typed their PIN). That exists so the panel can be reached WITHOUT
    closing the shift: logging out to become an admin ends the shift, which
    made a five-second barcode fix cost a close and a reopen.

    Elevation is stored on the session, so it dies with it -- on logout, and on
    any restart, since sessions live in memory.
    """
    s = require_session(sid)
    if s["role"] == "admin":
        return s
    if time.time() < (s.get("elevated_until") or 0):
        # Sliding idle window: still working, so keep it open.
        s["elevated_until"] = time.time() + ELEVATION_SECONDS
        return s
    s.pop("elevated_until", None)
    raise HTTPException(403, "admin_only")


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
    # Optional since 2026-08-24 -- a retiro no longer needs an admin. Kept in
    # the model so an older client that still sends one is not rejected.
    admin_pin: str | None = None


class CloseShiftIn(BaseModel):
    counted_cents: int = Field(ge=0)
    admin_pin: str | None = None


class VerifyPinIn(BaseModel):
    pin: str


# A shortfall beyond this needs an admin's PIN to close the shift. An overage
# never blocks closing -- only cash actually missing is a control problem.
SHORTFALL_REQUIRES_ADMIN_CENTS = -5000


# ------------------------------------------------------------------ routes

# --------------------------------------------------------------- error pages

_ERROR_PAGE = """<!doctype html><html lang=es><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Error</title>
<style>
 html,body{{margin:0;height:100%%;background:#0d1319;color:#e6edf3;
   font-family:system-ui,-apple-system,Segoe UI,sans-serif}}
 .w{{height:100%%;display:grid;place-items:center;text-align:center;padding:24px}}
 .c{{max-width:460px}}
 h1{{font-size:22px;margin:0 0 10px}}
 p{{color:#8b9bab;font-size:15px;margin:0 0 26px;line-height:1.5}}
 code{{color:#8b9bab;font-size:13px}}
 a{{display:inline-block;padding:15px 30px;border-radius:10px;background:#35d986;
   color:#0d1319;font-weight:700;font-size:16px;text-decoration:none}}
</style>
<div class=w><div class=c>
 <h1>{title}</h1>
 <p>{msg}</p>
 <a href="/">Volver a la caja</a>
 <p style="margin-top:22px"><code>{detail}</code></p>
</div></div>"""


@app.exception_handler(StarletteHTTPException)
async def page_errors(request: Request, exc: StarletteHTTPException):
    """
    Never dead-end the kiosk.

    Chromium runs with --kiosk: no address bar, no back button, no tabs. Any
    page navigation that returns FastAPI's default JSON error strands the
    cashier there until someone SSHes in -- which happened on 2026-08-24 when
    a restart invalidated the session and /admin returned {"detail":"no
    session"} as a bare JSON body.

    So: /api/* keeps returning JSON, because the front-end parses it. Every
    other path gets a real page with a way back. This catches 404s on mistyped
    paths and anything added later, not just the /admin case already fixed.
    """
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    title = "Página no encontrada" if exc.status_code == 404 else "Algo salió mal"
    msg = ("Esta pantalla no existe."
           if exc.status_code == 404 else
           "La caja sigue funcionando. Vuelve y, si hace falta, inicia sesión de nuevo.")
    return HTMLResponse(
        _ERROR_PAGE.format(title=title, msg=msg, detail="%s %s" % (exc.status_code, exc.detail)),
        status_code=exc.status_code)


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
            # Surfaced to the till, not just the admin page: a cashier who
            # does not know printing is off will keep pressing COBRAR and
            # wondering where the tickets are.
            "test_mode": db.test_mode(c),
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
    should still show that its printer has died.

    Carries catalogue_revision so the sell screen can notice a price change
    pushed from central and reload itself. Without this, a price edited in the
    console would not reach the cashier until someone restarted the kiosk --
    which is exactly the kind of "it says it works" gap that bites in March.
    """
    with conn() as c:
        rev = int(db.meta(c, "catalogue_revision", 0))
    return {**devices.status(), "sync": sync.status(), "catalogue_revision": rev}


@app.on_event("startup")
def _start_sync():
    # Draining runs on its own thread and is a no-op unless a backend URL is
    # configured, so a register with no backend behaves exactly as before.
    started = sync.start()
    if started:
        print("sync: draining to", sync.URL, "every", sync.INTERVAL, "s", flush=True)


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

        # --- side effects, AFTER the sale is committed -------------------
        # Order matters. The sale is already durable at this point, so
        # nothing below can lose it; both calls return (ok, detail) instead
        # of raising precisely so a dead printer cannot undo a paid sale.
        # The client is told what happened so the cashier can reach for the
        # key or reprint rather than discovering it from a customer.
        testing = db.test_mode(c)
        result["test_mode"] = testing
        if testing:
            result["printed"] = result["drawer"] = None
        else:
            ok_p, why_p = printer.print_receipt(
                result,
                store_name=db.meta(c, "store_name", "Tienda Balneario"),
                store_line2=db.meta(c, "store_line2", "Vista Hermosa - Caja 1"),
                cashier=s.get("name", ""))
            ok_d, why_d = devices.open_drawer()
            result["printed"], result["drawer"] = ok_p, ok_d
            if not ok_p:
                db.audit(c, "receipt_failed", by_user=s["id"],
                         detail={"sale": result["id"], "why": why_p})
            # Every drawer opening is on the record, including the automatic
            # one at checkout -- otherwise the audit trail only shows the
            # manual F16 presses and looks suspiciously sparse at close.
            db.audit(c, "drawer_opened", by_user=s["id"],
                     detail={"ok": ok_d, "device": why_d, "reason": "sale",
                             "sale": result["id"]})
        return result


@app.post("/api/receipt/reprint")
def receipt_reprint(sid: str | None = Cookie(default=None)):
    """
    Reprint the last ticket, marked *** COPIA ***.

    Not admin-gated: a customer asking for the ticket that did not come out is
    routine, and the copy is stamped so it cannot be passed off as a second
    sale. Audited so a run of reprints is still visible.
    """
    s = require_session(sid)
    with conn() as c:
        sale = db.last_sale(c)
        if sale is None:
            raise HTTPException(404, "no_sale_to_reprint")
        if db.test_mode(c):
            return {"ok": True, "test_mode": True, "seq": sale["seq"]}
        ok, why = printer.print_receipt(
            sale,
            store_name=db.meta(c, "store_name", "Tienda Balneario"),
            store_line2=db.meta(c, "store_line2", "Vista Hermosa - Caja 1"),
            cashier=sale.get("cashier") or "",
            reprint=True)
        db.audit(c, "receipt_reprinted", by_user=s["id"],
                 detail={"sale": sale["id"], "seq": sale["seq"], "ok": ok, "why": why})
        if not ok:
            raise HTTPException(503, why)
        return {"ok": True, "test_mode": False, "seq": sale["seq"]}


class DrawerIn(BaseModel):
    # Why the drawer was opened, so the audit trail can tell a cashier making
    # change from one counting down at close from one opening it for no stated
    # reason. Free-form and client-supplied: it is a label on an event that is
    # recorded either way, not a permission check.
    reason: str = "manual"


@app.post("/api/drawer/open")
def drawer_open(body: DrawerIn | None = None, sid: str | None = Cookie(default=None)):
    """
    Open the cash drawer. Any signed-in cashier, no admin override.

    Deliberately ungated, unlike /api/cash/drop below. A cashier needs the
    drawer for change on essentially every sale; a till that demands a manager
    for that is a till whose drawer gets propped open all morning, which is
    strictly worse than what the gate was protecting against. Accountability
    comes from the audit row instead -- who opened it, when, and whether the
    hardware actually fired.

    The audit row is written even when the kick fails, because "the drawer was
    opened and the printer was dead" and "nobody tried" are different stories
    and the reconciliation at close of shift depends on telling them apart.
    """
    s = require_session(sid)
    ok, detail = devices.open_drawer()
    with conn() as c:
        db.audit(c, "drawer_opened", by_user=s["id"],
                 detail={"ok": ok, "device": detail,
                         "reason": (body.reason if body else "manual")})
    if not ok:
        raise HTTPException(503, detail)
    return {"ok": True}


@app.post("/api/cash/drop")
def cash_drop(body: DropIn, sid: str | None = Cookie(default=None)):
    """
    Retiro parcial. No admin override.

    Changed 2026-08-24. The gate was costing more than it bought: the drawer
    already opens without an admin (a cashier needs it for change constantly),
    so requiring a PIN only to *record* the amount meant the realistic failure
    was cash leaving with no record at all, while a manager was found. An
    unrecorded retiro is invisible; a recorded one without a second signature
    is still fully attributable and shows up against the count at close.

    Both halves are on the record: the drawer opening is audited by
    /api/drawer/open with reason "retiro", and the amount lands here as a
    cash_movement plus its own audit row. The envelope number ties the paper
    in the safe to this row.
    """
    s = require_session(sid)
    with conn() as c:
        shift = db.current_shift(c)
        if not shift:
            raise HTTPException(409, "no_open_shift")
        env = db.next_envelope_no(c, shift["id"])
        mv = db.cash_movement(c, shift_id=shift["id"], kind="drop",
                              amount_cents=body.amount_cents, by_user=s["id"],
                              envelope_no=env)
        db.audit(c, "cash_drop", by_user=s["id"],
                 detail={"amount_cents": body.amount_cents, "envelope_no": env})
        return {**mv, "expected_cents": db.shift_expected_cents(c, shift["id"])}


class FloatInIn(BaseModel):
    amount_cents: int = Field(gt=0)


@app.post("/api/cash/float_in")
def cash_float_in(body: FloatInIn, sid: str | None = Cookie(default=None)):
    """
    Agregar efectivo -- cash going INTO the drawer. The mirror of a retiro.

    Ungated for the same reason /api/cash/drop is: the drawer already opens
    without an admin, so a PIN here would only mean cash gets added with no
    record while someone hunts for a manager. Recorded and attributable beats
    authorised and missing.

    NO envelope number. Envelopes tie a paper bag in the safe to money that
    LEFT; money arriving has no bag, and minting a number for it would put
    meaningless gaps in the retiro sequence that someone would later try to
    reconcile.

    The shift arithmetic needs nothing added: v_shift_expected already sums
    float_in positively and everything else negatively, so the expected drawer
    total moves the moment this row lands.
    """
    s = require_session(sid)
    with conn() as c:
        shift = db.current_shift(c)
        if not shift:
            raise HTTPException(409, "no_open_shift")
        mv = db.cash_movement(c, shift_id=shift["id"], kind="float_in",
                              amount_cents=body.amount_cents, by_user=s["id"])
        db.audit(c, "cash_float_in", by_user=s["id"],
                 detail={"amount_cents": body.amount_cents})
        return {**mv, "expected_cents": db.shift_expected_cents(c, shift["id"])}


class PowerIn(BaseModel):
    mode: str = Field(pattern="^(poweroff|reboot)$")


def _power_after_response(mode: str) -> None:
    """
    Let the HTTP response reach the browser before the machine goes down.

    Calling systemctl inline races the reply: the socket dies with the service
    and the cashier is left looking at a till that appears to have failed
    rather than one that is shutting down as asked.
    """
    def run():
        time.sleep(1.5)
        subprocess.run(["/usr/bin/systemctl", mode], check=False)
    threading.Thread(target=run, daemon=True).start()


@app.post("/api/power")
def power(body: PowerIn, sid: str | None = Cookie(default=None)):
    """
    Shut the till down (or restart it) from the UI.

    Why this exists: there was no way to power off from the screen, so the only
    route was the physical button on the box. A short press is in fact a clean
    logind poweroff, but nobody knew that, and holding the button cuts power in
    firmware -- which is what put 155 unsafe shutdowns on the NVMe and corrupted
    the filesystem once already.

    REFUSES WHILE A SHIFT IS OPEN. Powering off mid-shift would strand the count
    with cash in the drawer and nothing reconciled; closing the shift is the act
    that ends the day, and this belongs after it.

    Deliberately not admin-gated. The person closing up is the cashier, and an
    admin is not on site at closing time -- the same reasoning that removed the
    override from a retiro. Attribution comes from the audit row instead.

    Authorisation is a polkit rule scoped to this user and these actions
    (/etc/polkit-1/rules.d/50-cashregister-power.rules); the app holds no sudo.

    THE SESSION IS OPTIONAL, deliberately. The till sits at the login screen
    whenever nobody is serving, which is both the usual moment to turn it off
    and the exact state it was in when a cashier reached for the physical
    button on 2026-08-29. Requiring a login to shut down would have left that
    case unsolved and sent them back to the button.

    That is not an escalation. This API binds 127.0.0.1, so the only thing that
    can reach it is the kiosk browser on this machine, and anyone standing
    close enough to use it can already press the power button on the box. The
    open-shift check below is what actually protects the money, and it applies
    either way; when nobody is logged in the audit row simply carries a null
    user, which is still more than the physical button records.
    """
    s = _sessions.get(sid or "")
    with conn() as c:
        if db.current_shift(c):
            raise HTTPException(409, "shift_open")
        db.audit(c, "power_" + body.mode, by_user=(s["id"] if s else None),
                 detail={"mode": body.mode, "authenticated": bool(s)})
    _power_after_response(body.mode)
    return {"ok": True, "mode": body.mode}


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
        # Summary is gathered BEFORE printing but AFTER the close, so the
        # figures on paper are the ones actually written to the shift row.
        summary = db.shift_summary(c, shift["id"])
        printed = None
        if not db.test_mode(c):
            printed, why = printer.print_shift_report(
                summary, result,
                store_name=db.meta(c, "store_name", "Tienda Balneario"),
                store_line2=db.meta(c, "store_line2", "Vista Hermosa - Caja 1"),
                cashier=s.get("name", ""),
                authorized_by=(admin["name"] if authorized_by else ""))
            if not printed:
                # The shift is closed either way -- refusing to close because a
                # printer jammed would strand the till at the worst moment.
                db.audit(c, "corte_print_failed", by_user=s["id"],
                         detail={"shift": shift["id"], "why": why})
        return {**result,
                "printed": printed,
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
    """
    Serve the admin page, or bounce back to the till.

    This is a PAGE navigation, not an API call, and the difference matters:
    raising here renders FastAPI's raw JSON error into a browser running
    --kiosk, which has no address bar, no back button and no way out. A
    cashier who lands on it is stuck until someone SSHes into the till. Seen
    for real on 2026-08-24, after a service restart invalidated the in-memory
    session while the page still showed the admin link.

    Redirecting costs nothing: index.html already presents the login overlay
    when there is no session, which is exactly where this should end up.
    """
    try:
        require_admin_session(sid)
    except HTTPException:
        return RedirectResponse("/", status_code=303)
    return FileResponse(os.path.join(STATIC, "admin.html"))


class SettingsIn(BaseModel):
    test_mode: bool


@app.get("/api/admin/settings")
def get_settings(sid: str | None = Cookie(default=None)):
    require_admin_session(sid)
    with conn() as c:
        return {"test_mode": db.test_mode(c)}


@app.put("/api/admin/settings")
def put_settings(body: SettingsIn, sid: str | None = Cookie(default=None)):
    """Admin-only, and audited -- turning printing off is a thing you want a name against."""
    s = require_admin_session(sid)
    with conn() as c:
        db.set_meta(c, "test_mode", "1" if body.test_mode else "0")
        db.audit(c, "test_mode_changed", by_user=s["id"],
                 detail={"test_mode": body.test_mode})
        return {"test_mode": db.test_mode(c)}


class ElevateIn(BaseModel):
    pin: str = Field(min_length=4, max_length=12)


@app.post("/api/admin/elevate")
def admin_elevate(body: ElevateIn, sid: str | None = Cookie(default=None)):
    """
    Let the signed-in cashier into the admin panel on an admin's PIN, without
    logging out first.

    The point is the shift. Becoming an admin the old way meant logging the
    cashier out, which CLOSES THE SHIFT -- so fixing one barcode cost a close
    and a reopen, and in practice meant the barcode did not get fixed.

    The PIN is verified here, server-side, against every admin (require_admin,
    the same check a retiro override uses). An earlier version of the override
    trusted any six digits typed into the browser; that was a real bug and this
    must not repeat it.

    Both people end up on the record: the audit row carries the cashier who is
    holding the session and the admin whose PIN opened it.
    """
    s = require_session(sid)
    with conn() as c:
        admin = require_admin(c, body.pin)          # 403 override_denied on a bad PIN
        s["elevated_until"] = time.time() + ELEVATION_SECONDS
        db.audit(c, "admin_elevated", by_user=s["id"],
                 detail={"authorized_by": admin["id"], "seconds": ELEVATION_SECONDS})
    return {"ok": True, "seconds": ELEVATION_SECONDS}


@app.get("/api/admin/session")
def admin_session(sid: str | None = Cookie(default=None)):
    """So admin.html can confirm it's really allowed in, and show who's logged in."""
    s = require_admin_session(sid)
    # Once a backend is configured, central owns product identity and pricing
    # and pushes it down. Leaving these screens editable here would create two
    # masters and the next pull would silently overwrite whatever was typed --
    # so the client turns itself read-only for those fields instead. Barcodes
    # stay editable: the till still generates them and prints the labels.
    return {"session": s, "catalogue_managed_centrally": bool(sync.URL)}


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


@app.delete("/api/admin/products/{product_id}")
def admin_delete_product(product_id: int, sid: str | None = Cookie(default=None)):
    """
    Remove a product that has never been sold.

    Refuses with 409 `has_sales` otherwise -- see db.delete_product for why.
    The client turns that into an offer to deactivate instead, which is the
    right outcome for anything with history behind it.
    """
    s = require_admin_session(sid)
    with conn() as c:
        row = db.get_product_admin(c, product_id)
        name = row["name"] if row is not None else None
        result = db.delete_product(c, product_id)
        if result == "unknown":
            raise HTTPException(404, "unknown_product")
        if result == "has_sales":
            raise HTTPException(409, "has_sales")
        db.audit(c, "product_deleted", by_user=s["id"],
                 detail={"product_id": product_id, "name": name})
        return {"ok": True}


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
        # This till owns barcodes, so central has to be told promptly. Without
        # the nudge the change waits for the ten-minute reconcile, which is
        # long enough for someone to assume the scan failed and redo it.
        sync.mark_barcodes_dirty()
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
        sync.mark_barcodes_dirty()
        return db.get_product_admin(c, product_id)


class ReceivingLine(BaseModel):
    product_id: int
    qty: int
    unit_cost_cents: int | None = None


class ReceivingIn(BaseModel):
    lines: list[ReceivingLine]
    note: str | None = None


@app.post("/api/admin/receiving")
def admin_receiving(body: ReceivingIn, sid: str | None = Cookie(default=None)):
    """
    Record a delivery scanned in at the till.

    Admin-gated: this moves stock figures, and the same override that guards a
    price edit should guard a claim that 60 units arrived.

    Quantities may be negative -- breakage, a miscount, or undoing a
    double-entry -- but zero is refused, because a zero-qty line is always a
    mistake rather than a statement about the world.
    """
    s = require_admin_session(sid)
    if not body.lines:
        raise HTTPException(400, "no_lines")
    if any(l.qty == 0 for l in body.lines):
        raise HTTPException(400, "zero_qty")
    with conn() as c:
        for l in body.lines:
            if db.get_product_admin(c, l.product_id) is None:
                raise HTTPException(404, "unknown_product")
        rows = db.add_receiving(
            c, [l.model_dump() for l in body.lines],
            by_user=s["id"], note=(body.note or None))
        db.audit(c, "receiving", by_user=s["id"],
                 detail={"lines": len(rows), "units": sum(r["qty"] for r in rows)})
        # No nudge needed: the outbox drains unconditionally on the sync loop,
        # so this reaches central within one interval (30 s).
        return {"ok": True, "recorded": len(rows)}


@app.get("/api/admin/receiving")
def admin_receiving_recent(sid: str | None = Cookie(default=None)):
    require_admin_session(sid)
    with conn() as c:
        return {"recent": db.recent_receiving(c)}


@app.delete("/api/admin/barcodes/{code}")
def admin_delete_barcode(code: str, sid: str | None = Cookie(default=None)):
    require_admin_session(sid)
    with conn() as c:
        db.delete_barcode(c, bc.normalise(code))
        sync.mark_barcodes_dirty()
        return {"ok": True}

app.mount("/static", StaticFiles(directory=STATIC), name="static")
