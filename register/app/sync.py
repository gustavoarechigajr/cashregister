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

_last = {"at": None, "ok": None, "sent": 0, "error": None}


def status() -> dict:
    """What the last attempt did. Surfaced in /api/devices for the header."""
    return dict(_last, configured=bool(URL))


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


def _loop():
    while True:
        try:
            drain_once()
        except Exception as e:                    # never kill the thread
            _last.update(at=db.now_iso(), ok=False, error="loop: %s" % str(e)[:180])
        time.sleep(INTERVAL)


def start():
    """Begin draining in the background. No-op if no backend is configured."""
    if not URL:
        return False
    threading.Thread(target=_loop, name="sync-drain", daemon=True).start()
    return True
