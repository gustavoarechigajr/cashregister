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

# Pull counter. Reconciliation needs the full catalogue, so it cannot ride on
# the revision short-circuit -- see RECONCILE_EVERY below.
_pulls = 0
RECONCILE_EVERY = 20            # ~10 minutes at a 30 s interval


def status() -> dict:
    """What the last attempt did. Surfaced in /api/devices for the header."""
    return dict(_last, configured=bool(URL))


def _get(path: str) -> dict:
    req = urllib.request.Request(
        URL.rstrip("/") + path, method="GET",
        headers={"Authorization": "Bearer " + TOKEN})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def _post_to(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        URL.rstrip("/") + path,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + TOKEN})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def _post(payload: dict) -> dict:
    return _post_to("/api/sync", payload)


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
    global _pulls
    _pulls += 1
    con = db.connect()
    try:
        have = int(db.meta(con, "remote_catalogue_revision", -1))
        # Periodically ask for the full catalogue even when the revision has
        # not moved. Reconciliation compares against central's barcode list,
        # and the short-circuit does not send one -- so without this, a code
        # that exists only on the till would sit unnoticed until somebody
        # happened to edit a price.
        reconcile = (_pulls % RECONCILE_EVERY == 1)
        data = _get("/api/catalogue/pull?since=%d" % (-1 if reconcile else have))
        if not data.get("changed"):
            _last["catalogue_rev"] = data.get("revision")
            return False
        if reconcile and data.get("revision") == have:
            # Nothing to apply; this fetch existed only to reconcile barcodes.
            _push_orphan_barcodes(con, data)
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

        _push_orphan_barcodes(con, data)

        _last["catalogue_rev"] = data["revision"]
        _last["catalogue_at"] = db.now_iso()
        return True
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        _last["error"] = "catalogue: %s" % str(e)[:160]
        return False
    finally:
        con.close()


def _push_orphan_barcodes(con, data) -> int:
    """
    Send central any code this till has that central does not.

    The pull already carries central's full barcode list, so the diff is free
    -- no extra round trip and no cursor to keep. Central owns generation now,
    but a code can still exist only here: restored from a backup, or created
    before central took over. A code that scans at the till and is invisible in
    the console is a reporting hole, and this closes it on the next cycle.

    Only codes whose product central already knows are sent. Central would
    reject the rest anyway, and retrying them every 30 s forever would be
    noise, not resilience.
    """
    try:
        theirs = {b["code"] for b in (data.get("barcodes") or [])}
        known_products = {p["id"] for p in (data.get("products") or [])}
        mine = [dict(r) for r in con.execute(
            "SELECT code, product_id, is_internal FROM barcode")]
        orphans = [b for b in mine
                   if b["code"] not in theirs and b["product_id"] in known_products]
        if not orphans:
            return 0
        res = _post_to("/api/catalogue/barcodes/adopt",
                       {"barcodes": [{"code": b["code"], "product_id": b["product_id"],
                                      "is_internal": bool(b["is_internal"])}
                                     for b in orphans[:500]]})
        return int(res.get("adopted", 0))
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        _last["error"] = "adopt: %s" % str(e)[:160]
        return 0


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
