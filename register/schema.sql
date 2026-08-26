-- Cash register — local SQLite schema.
--
-- This database is authoritative for everything the register does. It runs the
-- store with no server present. The backend (Phase 5) later drains sync_outbox
-- and serves catalogue updates; nothing here changes when it arrives.
--
-- Conventions, all deliberate:
--   * Money is INTEGER centavos. Never floats — 0.1 + 0.2 must not decide change due.
--   * Timestamps are ISO-8601 UTC with offset ('2026-08-23T20:34:11+00:00').
--     The store is America/Mexico_City; display converts, storage never does.
--   * Ids that cross the wire are UUID text, generated here. Autoincrement ids
--     would collide the moment the server or a second register assigns one.
--   * Sales are immutable. A mistake is corrected by a compensating event,
--     never by UPDATE or DELETE. Triggers below enforce that.

PRAGMA journal_mode = WAL;      -- survives an abrupt power loss mid-sale
PRAGMA foreign_keys = ON;
PRAGMA synchronous = FULL;      -- a till in a shop with a flapping power feed

-- ---------------------------------------------------------------- meta

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- seeded: schema_version, register_id (uuid), catalogue_revision, ticket_seq

-- ------------------------------------------------------------ catalogue
-- Owned by the backend once it exists; seeded locally until then.
-- catalogue_revision in meta advances on every applied change, so the backend
-- can later serve deltas instead of full dumps.

CREATE TABLE IF NOT EXISTS category (
    id         TEXT PRIMARY KEY,          -- 'cerveza', 'botanas', ...
    name       TEXT NOT NULL,             -- 'Cerveza'
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS product (
    id           INTEGER PRIMARY KEY,     -- carried over from Aronium ids
    category_id  TEXT NOT NULL REFERENCES category(id),
    name         TEXT NOT NULL,
    price_cents  INTEGER NOT NULL CHECK (price_cents >= 0),
    cost_cents   INTEGER CHECK (cost_cents IS NULL OR cost_cents >= 0),
    is_active    INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    is_frequent  INTEGER NOT NULL DEFAULT 0 CHECK (is_frequent IN (0, 1)),
    sort_hint    INTEGER NOT NULL DEFAULT 0,   -- sales volume, drives grid order
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_product_category ON product(category_id, sort_hint DESC);
CREATE INDEX IF NOT EXISTS ix_product_frequent ON product(sort_hint DESC) WHERE is_frequent = 1;

-- One row per scannable code. PRIMARY KEY on the code makes it impossible for
-- two products to claim the same barcode — the failure that rings up the wrong
-- item and is nearly invisible afterwards.
--
-- `code` holds the NORMALISED form: UPC-A (12) is left-padded to 13 so it
-- matches its EAN-13 identity. EAN-8 stays 8. Lookup normalises the same way.
CREATE TABLE IF NOT EXISTS barcode (
    code        TEXT PRIMARY KEY,
    product_id  INTEGER NOT NULL REFERENCES product(id) ON DELETE CASCADE,
    is_internal INTEGER NOT NULL DEFAULT 0 CHECK (is_internal IN (0, 1)),
    printed     TEXT     -- original form, when it differed (e.g. the 12-digit UPC)
);

CREATE INDEX IF NOT EXISTS ix_barcode_product ON barcode(product_id);

-- ---------------------------------------------------------------- users

CREATE TABLE IF NOT EXISTS app_user (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('admin', 'cashier')),
    -- Argon2/bcrypt over the PIN. A 4-digit PIN is only 10 000 possibilities and
    -- hashing does not change that: lockout below is the real control.
    pin_hash        TEXT NOT NULL,
    is_active       INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until    TEXT,
    updated_at      TEXT NOT NULL
);

-- --------------------------------------------------------------- shifts

CREATE TABLE IF NOT EXISTS shift (
    id                  TEXT PRIMARY KEY,          -- uuid
    register_id         TEXT NOT NULL,
    user_id             INTEGER NOT NULL REFERENCES app_user(id),
    opened_at           TEXT NOT NULL,
    closed_at           TEXT,
    opening_float_cents INTEGER NOT NULL DEFAULT 0,
    -- filled at close
    counted_cents       INTEGER,
    expected_cents      INTEGER,
    difference_cents    INTEGER,
    closed_by           INTEGER REFERENCES app_user(id),
    authorized_by       INTEGER REFERENCES app_user(id)   -- required if short
);

CREATE INDEX IF NOT EXISTS ix_shift_open ON shift(closed_at) WHERE closed_at IS NULL;

-- ---------------------------------------------------------------- sales
-- Immutable. A refund is its own row referencing the original, never an edit.

CREATE TABLE IF NOT EXISTS sale (
    id              TEXT PRIMARY KEY,      -- uuid, generated here
    register_id     TEXT NOT NULL,
    shift_id        TEXT NOT NULL REFERENCES shift(id),
    user_id         INTEGER NOT NULL REFERENCES app_user(id),
    seq             INTEGER NOT NULL,      -- human ticket number, per register
    sold_at         TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'sale' CHECK (kind IN ('sale', 'refund')),
    total_cents     INTEGER NOT NULL,
    tendered_cents  INTEGER NOT NULL,
    change_cents    INTEGER NOT NULL,
    refunds_sale_id TEXT REFERENCES sale(id),
    authorized_by   INTEGER REFERENCES app_user(id),   -- admin who approved a refund
    UNIQUE (register_id, seq)
);

CREATE INDEX IF NOT EXISTS ix_sale_shift ON sale(shift_id);
CREATE INDEX IF NOT EXISTS ix_sale_date  ON sale(sold_at);

-- name and unit price are SNAPSHOTS. Prices change; a receipt reprinted next
-- season must show what was actually charged, not today's price.
CREATE TABLE IF NOT EXISTS sale_line (
    id               INTEGER PRIMARY KEY,
    sale_id          TEXT NOT NULL REFERENCES sale(id) ON DELETE CASCADE,
    product_id       INTEGER NOT NULL,        -- intentionally not FK: products may retire
    name_at_sale     TEXT NOT NULL,
    unit_price_cents INTEGER NOT NULL,
    qty              INTEGER NOT NULL CHECK (qty > 0),
    line_total_cents INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_line_sale    ON sale_line(sale_id);
CREATE INDEX IF NOT EXISTS ix_line_product ON sale_line(product_id);

-- ------------------------------------------------------- cash movements
-- Retiro parcial (drop), float in, payout. NOT shift close — that lives on shift.

CREATE TABLE IF NOT EXISTS cash_movement (
    id            TEXT PRIMARY KEY,        -- uuid
    register_id   TEXT NOT NULL,
    shift_id      TEXT NOT NULL REFERENCES shift(id),
    kind          TEXT NOT NULL CHECK (kind IN ('drop', 'float_in', 'payout')),
    amount_cents  INTEGER NOT NULL CHECK (amount_cents > 0),
    envelope_no   INTEGER,                 -- matches the bag in the safe
    by_user       INTEGER NOT NULL REFERENCES app_user(id),
    authorized_by INTEGER REFERENCES app_user(id),
    at            TEXT NOT NULL,
    note          TEXT
);

CREATE INDEX IF NOT EXISTS ix_cash_shift ON cash_movement(shift_id);

-- ---------------------------------------------------------------- audit
-- Every guarded action. In a cash-only shop this log plus the shift counts IS
-- the loss prevention — especially drawer opens with no sale behind them.

CREATE TABLE IF NOT EXISTS audit_event (
    id            TEXT PRIMARY KEY,        -- uuid
    register_id   TEXT NOT NULL,
    at            TEXT NOT NULL,
    action        TEXT NOT NULL,           -- drawer_open_no_sale, void_line, discount,
                                           -- refund, login_failed, override_denied, ...
    by_user       INTEGER REFERENCES app_user(id),
    authorized_by INTEGER REFERENCES app_user(id),
    detail        TEXT                     -- JSON
);

CREATE INDEX IF NOT EXISTS ix_audit_at ON audit_event(at);

-- ----------------------------------------------------------- sync outbox
-- Written from day one, drained only once the backend exists. Append-only, so
-- a link that is down for a week costs nothing but disk.

CREATE TABLE IF NOT EXISTS sync_outbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    entity     TEXT NOT NULL CHECK (entity IN ('sale', 'cash_movement', 'shift', 'audit_event', 'receiving')),
    entity_id  TEXT NOT NULL,
    payload    TEXT NOT NULL,              -- JSON snapshot, self-contained
    created_at TEXT NOT NULL,
    sent_at    TEXT,
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS ix_outbox_pending ON sync_outbox(id) WHERE sent_at IS NULL;

-- ------------------------------------------------------------ immutability
-- Enforced in the database, not only in application code. If a future bug or a
-- stray sqlite3 session tries to rewrite history, it fails loudly instead.

CREATE TRIGGER IF NOT EXISTS sale_no_update
BEFORE UPDATE ON sale
BEGIN
    SELECT RAISE(ABORT, 'sales are immutable — issue a refund instead');
END;

CREATE TRIGGER IF NOT EXISTS sale_no_delete
BEFORE DELETE ON sale
BEGIN
    SELECT RAISE(ABORT, 'sales are immutable — issue a refund instead');
END;

CREATE TRIGGER IF NOT EXISTS sale_line_no_update
BEFORE UPDATE ON sale_line
BEGIN
    SELECT RAISE(ABORT, 'sale lines are immutable — issue a refund instead');
END;

CREATE TRIGGER IF NOT EXISTS sale_line_no_delete
BEFORE DELETE ON sale_line
BEGIN
    SELECT RAISE(ABORT, 'sale lines are immutable — issue a refund instead');
END;

CREATE TRIGGER IF NOT EXISTS cash_movement_no_update
BEFORE UPDATE ON cash_movement
BEGIN
    SELECT RAISE(ABORT, 'cash movements are immutable');
END;

CREATE TRIGGER IF NOT EXISTS audit_no_update
BEFORE UPDATE ON audit_event
BEGIN
    SELECT RAISE(ABORT, 'audit events are immutable');
END;

CREATE TRIGGER IF NOT EXISTS audit_no_delete
BEFORE DELETE ON audit_event
BEGIN
    SELECT RAISE(ABORT, 'audit events are immutable');
END;

-- ------------------------------------------------------------------ views

-- What the drawer should hold right now, for the shift in progress.
CREATE VIEW IF NOT EXISTS v_shift_expected AS
SELECT
    s.id AS shift_id,
    s.opening_float_cents
      + COALESCE((SELECT SUM(CASE WHEN kind = 'refund' THEN -total_cents ELSE total_cents END)
                  FROM sale WHERE shift_id = s.id), 0)
      + COALESCE((SELECT SUM(CASE WHEN kind = 'float_in' THEN amount_cents ELSE -amount_cents END)
                  FROM cash_movement WHERE shift_id = s.id), 0)
      AS expected_cents
FROM shift s;

-- --------------------------------------------------- barcode tombstones
-- The till owns barcodes: the scanner is here, and every code is assigned by
-- scanning it onto a product. Central mirrors what this table and `barcode`
-- say. A deletion therefore has to be stated, not inferred -- central's copy
-- of a code the till dropped would otherwise be pushed straight back down on
-- the next pull, which is exactly what used to happen.
CREATE TABLE IF NOT EXISTS barcode_tombstone (
    code        TEXT PRIMARY KEY,
    deleted_at  TEXT NOT NULL
);

-- --------------------------------------------------------------- receiving
-- Stock arriving, recorded by scanning it in. Central computes on-hand as
-- received - sold, so this till never syncs a stock LEVEL -- only the event of
-- goods arriving, which can never conflict with another register's count.
--
-- Lives here rather than only on central because deliveries do not wait for
-- the network: the id is a uuid minted here and drains through sync_outbox
-- like a sale, so a delivery received on Wi-Fi that drops mid-count still
-- lands exactly once.
CREATE TABLE IF NOT EXISTS receiving (
    id              TEXT PRIMARY KEY,        -- uuid, minted here
    product_id      INTEGER NOT NULL REFERENCES product(id),
    qty             INTEGER NOT NULL,        -- negative allowed: breakage, miscount
    unit_cost_cents INTEGER,
    received_at     TEXT NOT NULL,
    by_user         INTEGER REFERENCES app_user(id),
    note            TEXT
);

CREATE INDEX IF NOT EXISTS ix_recv_product_till ON receiving(product_id);
