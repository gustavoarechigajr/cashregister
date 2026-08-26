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

# Set whenever this till writes a barcode. A push needs central's current
# barcode list to diff against, and only a full fetch (since=-1) carries one --
# so an edit has to ask for one rather than wait out the reconcile cadence.
# Without this a scan could sit unsent for ten minutes, which is long enough
# for someone to conclude it did not work and redo it.
_barcodes_dirty = False


def mark_barcodes_dirty():
    global _barcodes_dirty
    _barcodes_dirty = True


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

      * Infer a deletion from absence. A product or user missing from the
        snapshot is left alone rather than removed, because sale_line rows
        point at product ids and a till that quietly drops products mid-season
        is far worse than one carrying a stale row. Central marks things
        inactive instead, and that does come down.

        A hard delete is different: central names those ids explicitly in
        `deleted`, and they ARE removed here -- unless this till has sold the
        product, in which case it is deactivated instead so its sale_line rows
        keep something to point at.

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
        reconcile = _barcodes_dirty or (_pulls % RECONCILE_EVERY == 1)
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
            # Barcodes: insert-only, and never over a local decision.
            #
            # THIS TILL OWNS BARCODES. The scanner is here; every code is put on
            # a product by scanning it. So central's copy is a mirror, and a
            # pull may only ADD codes this till has not seen -- it must never
            # repoint one (that would undo a rescan onto a different product)
            # and never resurrect one this till deleted (that undid deletions a
            # few seconds after they were made).
            #
            # Central can still originate a code -- generate_barcode mints the
            # internal 2303311 series there, because the label sheet prints
            # there -- and that is exactly what this insert is for.
            for b in (data.get("barcodes") or []):
                con.execute(
                    "INSERT INTO barcode (code, product_id, is_internal) "
                    "SELECT ?,?,? WHERE NOT EXISTS "
                    "  (SELECT 1 FROM barcode_tombstone WHERE code = ?) "
                    "ON CONFLICT(code) DO NOTHING",
                    (b["code"], b["product_id"], 1 if b.get("is_internal") else 0,
                     b["code"]))
            # Tombstones. Central names the ids it hard-deleted; absence from
            # the snapshot still means nothing, so this is the only path that
            # removes a product here.
            #
            # A product this till has sold is kept and deactivated instead:
            # sale_line rows carry product_id, and the shift reports join on
            # it. Central refuses to delete anything with sales of its own, but
            # this till may hold sales that have not drained yet, so the check
            # has to be made locally too.
            for pid in (data.get("deleted") or []):
                sold = con.execute(
                    "SELECT 1 FROM sale_line WHERE product_id = ? LIMIT 1", (pid,)).fetchone()
                if sold:
                    con.execute("UPDATE product SET is_active = 0 WHERE id = ?", (pid,))
                else:
                    con.execute("DELETE FROM barcode WHERE product_id = ?", (pid,))
                    con.execute("DELETE FROM product WHERE id = ?", (pid,))

            # Users. Upsert identity, role, PIN and active flag -- but never
            # failed_attempts or locked_until, which are runtime state owned by
            # THIS till. Pushing those down would either clear a lockout that
            # is actively protecting the drawer, or apply one register's failed
            # attempts to another.
            for u in (data.get("users") or []):
                con.execute(
                    "INSERT INTO app_user (id, name, role, pin_hash, is_active, updated_at) "
                    "VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name, role=excluded.role,"
                    "  pin_hash=excluded.pin_hash, is_active=excluded.is_active,"
                    "  updated_at=excluded.updated_at",
                    (u["id"], u["name"], u["role"], u["pin_hash"],
                     1 if u.get("is_active") else 0, db.now_iso()))
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
    Push this till's barcode state up to central.

    The till owns barcodes, so this is not a gap-filler any more: it sends
    every code central is missing or has pointed at the wrong product, plus
    every code this till deleted. Central applies all of it verbatim.

    Sending only additions -- which is what this did before -- meant a deletion
    or a rescan onto a different product could not be expressed at all, and the
    next pull pushed central's stale copy back down over it. Adding a code was
    the only edit that survived a round trip.

    Still bounded by what central knows: a code whose product central has never
    heard of would violate its foreign key, so those wait for the product to
    exist rather than being retried into a rejection forever.
    """
    global _barcodes_dirty
    try:
        theirs = {b["code"]: b["product_id"] for b in (data.get("barcodes") or [])}
        known_products = {p["id"] for p in (data.get("products") or [])}
        mine = [dict(r) for r in con.execute(
            "SELECT code, product_id, is_internal FROM barcode")]
        # Missing there, or there but pointed somewhere else.
        push = [b for b in mine
                if b["product_id"] in known_products
                and theirs.get(b["code"]) != b["product_id"]]
        # Deletions only need sending while central still has the code.
        drop = [r["code"] for r in con.execute("SELECT code FROM barcode_tombstone")
                if r["code"] in theirs]
        if not push and not drop:
            _barcodes_dirty = False
            return 0
        res = _post_to("/api/catalogue/barcodes/adopt",
                       {"barcodes": [{"code": b["code"], "product_id": b["product_id"],
                                      "is_internal": bool(b["is_internal"])}
                                     for b in push[:500]],
                        "deleted": drop[:500]})
        # Only now: a failed post must leave the flag up so the next pull
        # retries rather than dropping the edit on the floor.
        _barcodes_dirty = False
        return int(res.get("adopted", 0)) + int(res.get("deleted", 0))
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
