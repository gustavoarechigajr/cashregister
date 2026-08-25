---
tags: [cashregister, backend, phase5]
---

# Central service — `trz-caja-16`

Phase 5 of [[PLAN]]. **Working end to end as of 2026-08-24.** The register drains
its outbox automatically; the console shows real sales, the full catalogue,
inventory with low-stock alerts, and printable date-range reports. The
catalogue **push-down** to the register is the main thing still missing.

## What exists

| | |
|---|---|
| Host | `trz-proxmox-13` (PVE 9.2.2) |
| Container | LXC **116**, `trz-caja-16`, Debian 13.6, unprivileged, `onboot 1` |
| Address | **`10.0.0.16/24`**, gw `10.0.0.2`, DNS `10.0.0.254`, domain `mgnt` |
| Resources | 2 cores, 2 GB RAM, 512 MB swap, 16 GB on `local-lvm` |
| Access | SSH key (`root@10.0.0.16`), Gus's `id_ed25519` installed at create |
| Database | PostgreSQL **17.11**, database `caja`, role `caja` owns every object |
| Schema | `schema.sql` in this directory — 11 tables, 2 reporting views |
| API | `caja-api.service` → `uvicorn` on **`0.0.0.0:8090`** |
| Secrets | `/etc/caja/env` (mode 640, `root:caja`): `CAJA_SYNC_TOKEN` for the till, `CAJA_ADMIN_PASSWORD` for the console |
| Console | `http://10.0.0.16:8090/` — password login, 12 h cookie session |

The `.16` address is not arbitrary: this fleet names containers after the last
octet (`trz-docker-14` → `.14`, `trz-vault-15` → `.15`), and `.16` sits inside
`ip dhcp excluded-address 10.0.0.1 10.0.0.30` on the gateway, so it can never
collide with a lease. See the Networking repo.

## Design decisions already baked into the schema

**Ingest must be idempotent.** Sales are keyed on the uuid the *register*
generated. Replaying an outbox — which happens every time a link drops
mid-drain — must never double-count a sale. This is what makes the one-way
event model safe.

**`sale.shift_id` is deliberately not a foreign key.** After an outage, outbox
rows can arrive in any order, and a sale must never be rejected because its
shift has not been ingested yet. Referential tidiness is worth less than not
losing a sale.

**User ids are not foreign keys either.** They are per-register integers;
central does not own the till's user table and must not fail ingest because a
cashier was renamed or removed.

**Stock is derived, never synced** — `v_stock_on_hand` computes
`received − sold`. The register's stock figures are a cashier-facing hint. This
is the rule that makes conflicts structurally impossible (see [[PLAN]], "Sync
model — sales are events, not state").

**LAN-only.** No Cloudflare Tunnel, per [[PLAN]]. If off-site access is ever
wanted, use the existing Tailscale tailnet.

## What comes next, in order

1. ✅ **Ingest API — done.** `POST /api/sync` takes a batch of `sync_outbox`
   rows and returns the highest outbox id now durable. Whole batch in one
   transaction: the register either advances its cursor past all of it or
   retries all of it, because a partially applied batch with an advanced
   cursor is how sales go missing. **Verified against the till's real queue:
   5 rows sent twice produced 3 sales, 4 lines, 1 shift — not doubles.**
   Get the token from `/etc/caja/env` on the container.
2. ✅ **Register-side sync client — done.** `register/app/sync.py` drains on a
   background thread every 30 s, configured by `/etc/cashregister/env`
   (`CASHREGISTER_SYNC_URL` / `_TOKEN` / `_INTERVAL`). `sent_at` is stamped
   only *after* the backend confirms, so a crash re-sends rather than skips —
   safe because ingest is idempotent. A register with no URL configured never
   drains and behaves exactly as before, which is correct for a till that must
   work with the network down. **Verified: 52 queued rows drained to 0.**

2b. ✅ **Reporting UI — done.** Served at `http://10.0.0.16:8090/`: summary
   (today / 7 days / all-time, sales by day, registers and last sync), ventas
   with expandable ticket lines, turnos y cortes with differences colour-coded,
   and a most-sold products ranking. Read-only by design — the till owns the
   catalogue until the push exists, and two masters is worse than one screen
   that cannot edit. Product names come from `sale_line` snapshots, not the
   empty `product` table.
2c. ✅ **Console — done.** Login, sidebar navigation, and six screens: Resumen
   (KPIs, sales chart, low-stock alerts, register heartbeats), Ventas
   (expandable ticket lines), Inventario (stock states + receiving), Catálogo
   (207 products, search, create/edit), Turnos, and Reportes (date range,
   by day / category / product, printable to PDF).

2d. ✅ **Catalogue seeded** from the till — 12 categories, 207 products, 237
   barcodes — via `tools/seed_catalogue.py`, which is idempotent and can be
   re-run while the till is still master.

3. ✅ **Catalogue push — done 2026-08-24. Central is now the master.**

   The till binds to `127.0.0.1`, so central cannot push; the register **pulls**
   on its existing 30 s sync cycle. `GET /api/catalogue/pull?since=<rev>`
   (sync-token auth — the caller is a daemon, not a browser) returns the whole
   catalogue when `meta.catalogue_revision` has moved, and nothing when it has
   not.

   **Full snapshot, not a delta.** ~200 products is a few tens of kilobytes,
   and a snapshot is immune to a missed increment; a delta scheme would need
   the register to have observed every intermediate revision, which an
   offline-first till cannot promise.

   Applied in one SQLite transaction, so the sell screen can never read a
   half-built catalogue. Deliberately **does not delete** — a product that
   disappears centrally is left alone rather than removed, because `sale_line`
   points at product ids and a till that silently drops products mid-season is
   worse than one carrying a stale row. Central marks things inactive instead,
   and that *does* come down. An empty pull is **refused** outright: it is far
   more likely to be a bug at the other end than a genuinely empty shop.

   **Barcodes moved here too (2026-08-24), and the reason is physical:** the
   label sheet prints on an ordinary printer, and there is no ordinary printer
   in the store — the admin prints from wherever they are, which is this
   console. So central mints the codes (continuing the same `2303311xxxxx`
   GS1 in-store series, derived from existing codes rather than a counter so
   the two sides cannot drift) and they ride down with the catalogue.
   The **Etiquetas** screen shows what lacks a code, generates one, and builds
   a printable sheet with EAN-13 drawn as inline SVG.

   Codes are **merged, never replaced**: a code that exists on the till but
   not centrally is left alone, because losing a working code is worse than
   carrying a spare.

   The running till notices without a restart: `catalogue_revision` rides along
   in `/api/devices` (already polled every 5 s) and the sell screen reloads its
   catalogue when it moves. **The cart is deliberately untouched** — lines
   already rung up keep the price the customer was quoted.

   The till's own admin screens turn **read-only** for products, prices and
   categories whenever a backend is configured, with a banner pointing at the
   console. Barcode assignment and label printing stay enabled.

   Verified end to end: price changed in the console → till had it within one
   cycle, local revision advanced, `/api/devices` served the new number.

4. **Receiving is built but unused.** Every product currently reads
   *sin seguimiento* because no reorder levels or opening stock have been
   entered. That is honest rather than broken: on-hand is `received − sold`, so
   until someone records what is actually on the shelf, central genuinely does
   not know. Setting a reorder level on a product starts tracking it.

⚠️ **The UI has no authentication.** LAN-only per [[PLAN]], and VLAN 10 is
already the trust boundary, but anyone on that VLAN can read four years of
sales data. Worth a shared password or Tailscale-only binding before this holds
a full season.

⚠️ **Not started, and worth remembering before the store move:** on Wi-Fi the
register sits on VLAN 50, where `ACL-USERS-IN` denies `10.0.50.0/24 →
10.0.0.0/24`. The till could not reach `10.0.0.16` over the Wi-Fi standby
without a permit for its address. Over Ethernet on VLAN 10 it is fine.

## Operations

```bash
ssh root@10.0.0.16
su - postgres -c 'psql -d caja'          # schema owner is the caja role
pct start 116 / pct stop 116             # from trz-proxmox-13
```

No backups configured yet. Phase 4 covers the register's own dumps; this
container needs its own `pg_dump` schedule before it holds anything real.
