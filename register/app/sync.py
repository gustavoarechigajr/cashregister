"""
Outbox drain — the register's half of the sync.

Design rules, in order of importance:

1. **A sale must never wait on this.** The drain runs on its own thread and
   touches the database only in short bursts. Nothing in the sell path calls
   into here, and no failure here is visible to a cashier.

2. **Losing a row is worse than sending it twice.** `sent_at` is stamped only
   after the backend has confirmed the batch, so a crash or a dropped
   connection at any point re-sends rather than skips. The backend is
   idempotent on the register-generated uuid precisely so this is safe.

3. **Silence is the normal state.** The link to the backend is expected to be
   down -- that is the whole premise of the project. A failed drain logs at
   debug and tries again later; it never raises, never retries in a tight
   loop, and never fills the journal.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request

from . import db

URL = os.environ.get("CASHREGISTER_SYNC_URL", "")
TOKEN = os.environ.get("CASHREGISTER_SYNC_TOKEN", "")
INTERVAL = int(os.environ.get("CASHREGISTER_SYNC_INTERVAL", "30"))
BATCH = 200          # keep a single POST small enough to finish over a poor link
TIMEOUT = 15

_last = {"at": None, "ok": None, "sent": 0, "error": None,
         "catalogue_rev": None, "catalogue_at": None}


def status() -> dict:
    """What the last attempt did. Surfaced in /api/devices for the header."""
    return dict(_last, configured=bool(URL))


def _get(path: str) -> dict:
    req = urllib.request.Request(
        URL.rstrip("/") + path, method="GET",
        headers={"Authorization": "Bearer " + TOKEN})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def _post(payload: dict) -> dict:
    req = urllib.request.Request(
        URL.rstrip("/") + "/api/sync",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + TOKEN})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def drain_once() -> int:
    """
    Send one batch. Returns the number of rows the backend accepted.

    Opens its own connection: this runs on a background thread and must not
    share the request-scoped one.
    """
    if not URL:
        return 0
    con = db.connect()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT id, entity, entity_id, payload, created_at FROM sync_outbox "
            "WHERE sent_at IS NULL ORDER BY id LIMIT ?", (BATCH,))]
        if not rows:
            # Nothing queued: send an empty batch as a heartbeat rather than
            # returning silently. Central otherwise cannot tell a till that is
            # idle from one that has stopped talking, and "no sales yet today"
            # is a completely normal state for this shop.
            try:
                _post({"register_id": db.meta(con, "register_id"),
                       "register_name": db.meta(con, "register_name", "tienda"),
                       "rows": []})
                _last.update(at=db.now_iso(), ok=True, sent=0, error=None)
            except (urllib.error.URLError, OSError, ValueError) as e:
                _last.update(at=db.now_iso(), ok=False, sent=0, error=str(e)[:200])
            return 0

        body = {
            "register_id": db.meta(con, "register_id"),
            "register_name": db.meta(con, "register_name", "tienda"),
            "rows": [{"id": r["id"], "entity": r["entity"],
                      "entity_id": r["entity_id"],
                      "payload": json.loads(r["payload"]),
                      "created_at": r["created_at"]} for r in rows],
        }
        result = _post(body)

        # Only now is it safe to stamp these. Ordering matters: if the process
        # dies between the POST and this UPDATE, the rows are re-sent and the
        # backend de-duplicates. The reverse ordering would lose them silently.
        ids = [r["id"] for r in rows]
        con.execute(
            "UPDATE sync_outbox SET sent_at = ? WHERE id IN (%s)"
            % ",".join("?" * len(ids)),
            [db.now_iso()] + ids)
        _last.update(at=db.now_iso(), ok=True, sent=len(ids), error=None)
        return int(result.get("applied", len(ids)))
    except (urllib.error.URLError, OSError, ValueError) as e:
        # The expected case, not an exception worth shouting about: the
        # backend is unreachable, or the store's link is down.
        con.execute(
            "UPDATE sync_outbox SET attempts = attempts + 1, last_error = ? "
            "WHERE sent_at IS NULL AND id IN ("
            "  SELECT id FROM sync_outbox WHERE sent_at IS NULL ORDER BY id LIMIT ?)",
            (str(e)[:200], BATCH))
        _last.update(at=db.now_iso(), ok=False, sent=0, error=str(e)[:200])
        return 0
    finally:
        con.close()


def pull_catalogue() -> bool:
    """
    Fetch the catalogue from central and apply it locally. Returns True if
    anything changed.

    Central owns product identity, price, cost, category and active state.
    This is the direction PLAN.md always intended: the register receives.

    What it deliberately does NOT do:

      * Delete. A product that vanishes centrally is left alone here rather
        than removed, because sale_line rows point at product ids and a till
        that quietly drops products mid-season is far worse than one carrying
        a stale row. Central marks things inactive instead, and that does
        come down.

      * Remove barcodes it does not know about. Codes are merged, never
        replaced wholesale: a code assigned on the till just before a pull
        must not vanish because central had not heard of it yet. Central owns
        generation, but losing a working code is worse than carrying a spare.

    Applied in one transaction so the sell screen can never read a half-built
    catalogue.
    """
    if not URL:
        return False
    con = db.connect()
    try:
        have = int(db.meta(con, "remote_catalogue_revision", -1))
        data = _get("/api/catalogue/pull?since=%d" % have)
        if not data.get("changed"):
            _last["catalogue_rev"] = data.get("revision")
            return False

        cats = data.get("categories") or []
        prods = data.get("products") or []
        # A pull that arrives empty is far more likely to be a bug at the other
        # end than a genuinely empty shop, and applying it would wipe the till.
        if not prods:
            _last.update(error="refused empty catalogue pull")
            return False

        con.execute("BEGIN IMMEDIATE")
        try:
            for c in cats:
                con.execute(
                    "INSERT INTO category (id, name, sort_order) VALUES (?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name,"
                    "  sort_order=excluded.sort_order",
                    (c["id"], c["name"], c.get("sort_order") or 0))
            for p in prods:
                con.execute(
                    "INSERT INTO product (id, category_id, name, price_cents, cost_cents,"
                    "                     is_active, updated_at) VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET category_id=excluded.category_id,"
                    "  name=excluded.name, price_cents=excluded.price_cents,"
                    "  cost_cents=excluded.cost_cents, is_active=excluded.is_active,"
                    "  updated_at=excluded.updated_at",
                    (p["id"], p["category_id"], p["name"], p["price_cents"],
                     p.get("cost_cents"), 1 if p.get("is_active") else 0, db.now_iso()))
            # Barcodes: upsert only. A code that exists locally but not
            # centrally is left alone (see the note above); a code whose owner
            # changed centrally is repointed, because two products claiming one
            # code would make scanning ambiguous.
            for b in (data.get("barcodes") or []):
                con.execute(
                    "INSERT INTO barcode (code, product_id, is_internal) VALUES (?,?,?) "
                    "ON CONFLICT(code) DO UPDATE SET product_id=excluded.product_id,"
                    "  is_internal=excluded.is_internal",
                    (b["code"], b["product_id"], 1 if b.get("is_internal") else 0))
            db.set_meta(con, "remote_catalogue_revision", data["revision"])
            # Bumping the LOCAL revision is what makes the running till notice:
            # the sell screen polls it and reloads rather than serving old
            # prices until someone restarts the kiosk.
            db.set_meta(con, "catalogue_revision",
                        int(db.meta(con, "catalogue_revision", 0)) + 1)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

        _last["catalogue_rev"] = data["revision"]
        _last["catalogue_at"] = db.now_iso()
        return True
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        _last["error"] = "catalogue: %s" % str(e)[:160]
        return False
    finally:
        con.close()


def _loop():
    while True:
        try:
            drain_once()
            pull_catalogue()
        except Exception as e:                    # never kill the thread
            _last.update(at=db.now_iso(), ok=False, error="loop: %s" % str(e)[:180])
        time.sleep(INTERVAL)


def start():
    """Begin draining in the background. No-op if no backend is configured."""
    if not URL:
        return False
    threading.Thread(target=_loop, name="sync-drain", daemon=True).start()
    return True
