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
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS barcode (
    code        text PRIMARY KEY,
    product_id  integer NOT NULL REFERENCES product(id) ON DELETE CASCADE,
    is_internal boolean NOT NULL DEFAULT false
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
    note         text
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

CREATE OR REPLACE VIEW v_stock_on_hand AS
SELECT p.id                AS product_id,
       p.name,
       COALESCE(r.qty, 0)  AS received,
       COALESCE(s.qty, 0)  AS sold,
       COALESCE(r.qty, 0) - COALESCE(s.qty, 0) AS on_hand
FROM product p
LEFT JOIN (SELECT product_id, SUM(qty) qty FROM receiving GROUP BY product_id) r
       ON r.product_id = p.id
LEFT JOIN (SELECT sl.product_id, SUM(sl.qty) qty
             FROM sale_line sl JOIN sale sa ON sa.id = sl.sale_id
            WHERE sa.kind = 'sale'
            GROUP BY sl.product_id) s
       ON s.product_id = p.id;

CREATE OR REPLACE VIEW v_sales_by_day AS
SELECT (sold_at AT TIME ZONE 'America/Mexico_City')::date AS day,
       register_id,
       COUNT(*)            AS tickets,
       SUM(total_cents)    AS total_cents
FROM sale
WHERE kind = 'sale'
GROUP BY 1, 2;
