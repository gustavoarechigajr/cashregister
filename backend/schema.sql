-- Central service schema (Postgres 17) for the store cash register.
--
-- Mirrors the register's SQLite schema where it must, and deliberately differs
-- where central has a different job:
--
--   * Sales are EVENTS that arrive, never rows that get edited. Ingest is
--     idempotent on the register's own uuid, so replaying an outbox -- which
--     happens whenever a link drops mid-drain -- can never double-count a sale.
--     This is the whole reason the sync model is one-way (see PLAN.md).
--
--   * Stock is DERIVED here (received - sold), never synced. The register's
--     stock numbers are a cached hint for the cashier; this is the truth.
--
--   * user ids are per-register integers, so they are stored alongside
--     register_id and are NOT foreign keys. Central does not own the till's
--     user table and must not break ingest because a cashier was renamed.
--
--   * Timestamps are timestamptz. The register sends ISO-8601 with an explicit
--     offset, so nothing is ambiguous on the wire.

CREATE TABLE IF NOT EXISTS register (
    id          uuid PRIMARY KEY,
    name        text,
    first_seen  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------------- meta
-- catalogue_revision is bumped on every catalogue write. The register polls it
-- and pulls only when it differs, so an unchanged catalogue costs one integer
-- comparison every 30 s instead of shipping 207 products.

CREATE TABLE IF NOT EXISTS meta (
    key   text PRIMARY KEY,
    value text NOT NULL
);

INSERT INTO meta (key, value) VALUES ('catalogue_revision', '1')
    ON CONFLICT (key) DO NOTHING;

-- --------------------------------------------------------------- catalogue
-- Central owns the catalogue; registers receive it. Kept simple until the
-- backend-hosted admin screens exist (PLAN.md Phase 6, second half).

-- Category ids are TEXT slugs ('cerveza', 'botanas'), matching the register's
-- schema. They are carried across verbatim rather than renumbered: an id that
-- means the same thing on both sides is worth more than a tidy integer key.
CREATE TABLE IF NOT EXISTS category (
    id         text PRIMARY KEY,
    name       text NOT NULL,
    sort_order integer NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS product (
    id            integer PRIMARY KEY,        -- carried over from the register
    category_id   text REFERENCES category(id),
    name          text    NOT NULL,
    price_cents   bigint  NOT NULL,
    cost_cents    bigint,
    is_active     boolean NOT NULL DEFAULT true,
    -- Reorder threshold for the low-stock view. NULL means "not tracked",
    -- which is the honest default: plenty of what this shop sells (ice, loose
    -- cups) will never have a meaningful count.
    reorder_level integer,
    -- Pack/single stock pooling. A six-pack of Corona and a single bottle are
    -- two sellable products drawing on ONE physical stock of bottles; the same
    -- is true of a 50-cup paquete and a loose cup. Without this the two halves
    -- drift apart the moment either one moves -- and they already had:
    -- Plato Individual sat at -12 while 18 unopened paquetes were on the shelf.
    --
    --   stock_product_id  the product whose units this one is counted in.
    --                     NULL means "itself", which is the correct no-op for
    --                     every ordinary product and keeps this a pure addition.
    --   units_per_sale    how many of those base units one of THIS is.
    --                     1 for a single, 6 for a six-pack, 50 for a paquete.
    --
    -- The multiplier applies to receiving as well as to sales. That is not a
    -- nicety: disposables arrive as paquetes and leave as singles, so a
    -- sales-only multiplier would report 29 cups where there are 1450.
    stock_product_id integer REFERENCES product(id),
    units_per_sale   integer NOT NULL DEFAULT 1 CHECK (units_per_sale > 0),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS barcode (
    code        text PRIMARY KEY,
    product_id  integer NOT NULL REFERENCES product(id) ON DELETE CASCADE,
    is_internal boolean NOT NULL DEFAULT false
);

-- ------------------------------------------------------------------- users
-- Central owns cashier and admin accounts and pushes them to the registers.
-- pin_hash is the register's own scrypt format, produced verbatim here so the
-- till can verify it without re-hashing anything.
--
-- failed_attempts and locked_until deliberately do NOT live here: lockout is
-- runtime state belonging to the machine where the PIN was actually typed, and
-- pushing it down would either clear a lockout or apply one register's
-- failures to another.

CREATE TABLE IF NOT EXISTS app_user (
    id         integer PRIMARY KEY,
    name       text NOT NULL,
    role       text NOT NULL CHECK (role IN ('admin', 'cashier')),
    pin_hash   text NOT NULL,
    is_active  boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------------ shifts

CREATE TABLE IF NOT EXISTS shift (
    id                  uuid PRIMARY KEY,
    register_id         uuid NOT NULL REFERENCES register(id),
    user_id             integer,
    -- Nullable on purpose. A shift is emitted twice (open, then close) and the
    -- close payload carries only closing fields. Postgres checks NOT NULL on
    -- the proposed tuple BEFORE the ON CONFLICT arbiter runs, so a NOT NULL
    -- here rejects every close upsert -- even when the row already exists and
    -- would merely have been updated. Same reasoning as sale.shift_id: an
    -- event arriving out of order must never be refused.
    opened_at           timestamptz,
    closed_at           timestamptz,
    opening_float_cents bigint NOT NULL DEFAULT 0,
    counted_cents       bigint,
    expected_cents      bigint,
    difference_cents    bigint,
    closed_by           integer,
    authorized_by       integer
);

CREATE INDEX IF NOT EXISTS ix_shift_register ON shift(register_id, opened_at);

-- ------------------------------------------------------------------- sales

CREATE TABLE IF NOT EXISTS sale (
    id              uuid PRIMARY KEY,
    register_id     uuid NOT NULL REFERENCES register(id),
    shift_id        uuid,
    user_id         integer,
    seq             integer NOT NULL,
    sold_at         timestamptz NOT NULL,
    kind            text NOT NULL DEFAULT 'sale' CHECK (kind IN ('sale', 'refund')),
    total_cents     bigint NOT NULL,
    tendered_cents  bigint NOT NULL,
    change_cents    bigint NOT NULL,
    refunds_sale_id uuid,
    authorized_by   integer,
    received_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (register_id, seq)
);

CREATE INDEX IF NOT EXISTS ix_sale_sold_at ON sale(sold_at);
CREATE INDEX IF NOT EXISTS ix_sale_shift   ON sale(shift_id);

-- shift_id is intentionally NOT a foreign key: outbox rows can arrive in any
-- order after an outage, and a sale must never be rejected because its shift
-- has not been ingested yet. Referential tidiness is worth less than not
-- losing a sale.

CREATE TABLE IF NOT EXISTS sale_line (
    sale_id          uuid    NOT NULL REFERENCES sale(id) ON DELETE CASCADE,
    line_no          integer NOT NULL,
    product_id       integer NOT NULL,
    name_at_sale     text    NOT NULL,
    unit_price_cents bigint  NOT NULL,
    qty              integer NOT NULL CHECK (qty > 0),
    line_total_cents bigint  NOT NULL,
    PRIMARY KEY (sale_id, line_no)
);

CREATE INDEX IF NOT EXISTS ix_line_product ON sale_line(product_id);

-- ---------------------------------------------------------- cash movements

CREATE TABLE IF NOT EXISTS cash_movement (
    id            uuid PRIMARY KEY,
    register_id   uuid NOT NULL REFERENCES register(id),
    shift_id      uuid,
    kind          text NOT NULL,
    amount_cents  bigint NOT NULL,
    envelope_no   integer,
    by_user       integer,
    authorized_by integer,
    at            timestamptz NOT NULL,
    note          text,
    received_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_cash_shift ON cash_movement(shift_id);

-- ------------------------------------------------------------------ audit

CREATE TABLE IF NOT EXISTS audit_event (
    id            uuid PRIMARY KEY,
    register_id   uuid NOT NULL REFERENCES register(id),
    at            timestamptz NOT NULL,
    action        text NOT NULL,
    by_user       integer,
    authorized_by integer,
    detail        jsonb,
    received_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_audit_at     ON audit_event(at);
CREATE INDEX IF NOT EXISTS ix_audit_action ON audit_event(action);

-- -------------------------------------------------------------- receiving
-- The other half of stock. Central computes on-hand as received - sold, which
-- is why the register never needs to sync a stock level and conflicts cannot
-- arise (PLAN.md, "Sync model").

CREATE TABLE IF NOT EXISTS receiving (
    id           bigserial PRIMARY KEY,
    product_id   integer NOT NULL REFERENCES product(id),
    qty          integer NOT NULL,
    unit_cost_cents bigint,
    received_at  timestamptz NOT NULL DEFAULT now(),
    note         text,
    -- The till's own uuid for this row, when it came from a scan-in rather
    -- than the console. UNIQUE is the whole point: a batch re-sent after an
    -- unacknowledged post must not add the stock twice. NULL for rows created
    -- here, and many NULLs are fine -- Postgres does not collide on them.
    source_id    uuid UNIQUE
);

CREATE INDEX IF NOT EXISTS ix_recv_product ON receiving(product_id);

-- ------------------------------------------------------------ sync ledger
-- What has been accepted, so the register can be told where to resume and a
-- human can see whether a till has gone quiet.

CREATE TABLE IF NOT EXISTS sync_batch (
    id           bigserial PRIMARY KEY,
    register_id  uuid NOT NULL REFERENCES register(id),
    received_at  timestamptz NOT NULL DEFAULT now(),
    rows_sent    integer NOT NULL,
    rows_applied integer NOT NULL,
    max_outbox_id bigint
);

CREATE INDEX IF NOT EXISTS ix_batch_register ON sync_batch(register_id, received_at);

-- --------------------------------------------------------------- reporting

-- Stock, pooled across pack/single pairs.
--
-- Every product belongs to exactly one POOL, counted in the pool's base unit
-- (a bottle, a cup, a fork). An ordinary product is its own pool with a
-- multiplier of 1, so for the ~190 products that are not part of a pair this
-- computes exactly what the old view did.
--
-- The multiplier is applied on BOTH sides of the subtraction. Beer arrives as
-- singles and may leave as six-packs; disposables arrive as paquetes and leave
-- as singles. A sales-only multiplier gets the second family wrong by 50x.
--
-- Columns come in two flavours:
--   *_base / on_hand_base  the pool's true balance in base units -- exact, and
--                          identical for every product sharing the pool.
--   received / sold / on_hand
--                          the same figures divided into THIS product's own
--                          unit, so a paquete row reads "17 paquetes" rather
--                          than "888 platos". Division floors, so these are
--                          "how many whole ones could I sell"; use the _base
--                          columns for arithmetic that has to balance.
CREATE OR REPLACE VIEW v_stock_on_hand AS
WITH m AS (
    SELECT id                             AS product_id,
           COALESCE(stock_product_id, id) AS pool_id,
           units_per_sale                 AS units
      FROM product
),
recv_agg AS (
    SELECT m.pool_id, SUM(r.qty * m.units) AS qty
      FROM receiving r
      JOIN m ON m.product_id = r.product_id
     GROUP BY m.pool_id
),
sold_agg AS (
    SELECT m.pool_id, SUM(sl.qty * m.units) AS qty
      FROM sale_line sl
      JOIN sale sa ON sa.id = sl.sale_id
      JOIN m       ON m.product_id = sl.product_id
     WHERE sa.kind = 'sale'
     GROUP BY m.pool_id
)
-- Column ORDER matters: the first five must stay exactly as the original view
-- had them, so CREATE OR REPLACE VIEW keeps working and this file stays safe to
-- re-run. New columns are appended, never inserted.
SELECT p.id            AS product_id,
       p.name,
       FLOOR(COALESCE(recv_agg.qty, 0)::numeric / m.units)::bigint AS received,
       FLOOR(COALESCE(sold_agg.qty, 0)::numeric / m.units)::bigint AS sold,
       FLOOR((COALESCE(recv_agg.qty, 0) - COALESCE(sold_agg.qty, 0))::numeric
             / m.units)::bigint                              AS on_hand,
       m.pool_id,
       m.units         AS units_per_sale,
       COALESCE(recv_agg.qty, 0)                             AS received_base,
       COALESCE(sold_agg.qty, 0)                             AS sold_base,
       COALESCE(recv_agg.qty, 0) - COALESCE(sold_agg.qty, 0) AS on_hand_base
FROM product p
JOIN m            ON m.product_id = p.id
LEFT JOIN recv_agg ON recv_agg.pool_id = m.pool_id
LEFT JOIN sold_agg ON sold_agg.pool_id = m.pool_id;

CREATE OR REPLACE VIEW v_sales_by_day AS
SELECT (sold_at AT TIME ZONE 'America/Mexico_City')::date AS day,
       register_id,
       COUNT(*)            AS tickets,
       SUM(total_cents)    AS total_cents
FROM sale
WHERE kind = 'sale'
GROUP BY 1, 2;

-- ------------------------------------------------------- deletion tombstones
-- The register's catalogue pull is upsert-only: it never removes a row it was
-- not told about, because a till that silently drops products mid-season is
-- worse than one carrying a stale one. So a hard delete here has to be stated
-- explicitly, and this is where it is stated. Rows are kept forever: they are
-- two integers each, and a till that has been offline for a season still has
-- to learn what went away while it was gone.
CREATE TABLE IF NOT EXISTS deleted_product (
    id          integer PRIMARY KEY,
    deleted_at  timestamptz NOT NULL DEFAULT now()
);
