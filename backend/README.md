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
| Console | **`http://tienda.mgnt`** (or `http://10.0.0.16:8090/`) — password login, 12 h cookie session |
| DNS name | `tienda.mgnt` → AdGuard rewrite to `10.0.0.254`, Apache vhost proxies to `:8090`. **Restricted to `10.0.0.0/24`** — VLAN 50 can reach that proxy for other names, so without the restriction the console would be open to the house Wi-Fi. HTTP only: the internal cert's SANs are an explicit list and do not include `tienda.mgnt`. |
| Password | Changed **in the UI** (Usuarios → Cambiar contraseña). Stored as an scrypt hash in `meta.admin_password`; `CAJA_ADMIN_PASSWORD` in the env file is the **bootstrap only**, used until one is set from the console |

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

**The till uses the IP, not the name.** `CASHREGISTER_SYNC_URL` stays
`http://10.0.0.16:8090` deliberately: an offline-first register must not gain a
DNS dependency, least of all on a service that sits on a different VLAN from
the store. `tienda.mgnt` is for humans.

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

   ⚠️ **Barcodes are the exception: the TILL owns them (revised 2026-08-25).**

   Between 2026-08-24 and 2026-08-25 central owned them, and the two halves of
   that decision contradicted each other. The pull repointed a code whose owner
   differed and re-inserted any code the till had deleted, while the upward
   reconcile could only *add*. Net effect: assigning a new code round-tripped,
   but **deleting one or moving it to another product silently reverted** on the
   next pull, seconds after the edit. An afternoon of barcode work was undone
   twice before the cause was found.

   Ownership now follows the hardware. The scanner is at the till, every code is
   assigned by scanning it onto a product, and central mirrors what the till
   says:

   * **The pull is insert-only for barcodes.** It may add a code the till has
     never seen — this is how central's generated codes arrive — but it never
     repoints one and never resurrects one listed in `barcode_tombstone`.
   * **Deletions are stated, not inferred.** `barcode_tombstone` on the till is
     what makes a removal survive a round trip.
   * **The push carries intent.** `/api/catalogue/barcodes/adopt` receives codes
     central lacks *or has pointed at the wrong product*, plus the tombstones,
     and applies all of it (`ON CONFLICT DO UPDATE`, plus deletes).
   * **A barcode edit sets a dirty flag**, so the next pull asks for the full
     catalogue and pushes immediately — ~30 s rather than waiting out the
     10-minute reconcile. Long enough to think a scan had failed and redo it.

   `tools/check-barcode-sync.sh` compares both sides read-only and exits
   non-zero when they disagree.

   **Central still mints the internal series.** The label sheet prints on an
   ordinary printer and there is none in the store, so generation stays here —
   continuing the same `2303311xxxxx` GS1 in-store range, derived from existing
   codes rather than a counter so the two sides cannot drift. Two machines
   numbering independently would hand one code to two products. Those codes ride
   down with the catalogue and come back up unchanged.

   The **Etiquetas** screen has three modes: **Completa** (whole catalogue in
   binder layout), **Carpeta** (just the selected codes, same layout) and
   **Recortar** (the original stick-on sheet). The binder is scanned off the
   page at the counter, so it uses true millimetre-sized barcodes with a locked
   aspect ratio, one category per page, and a binding margin.

   The running till notices without a restart: `catalogue_revision` rides along
   in `/api/devices` (already polled every 5 s) and the sell screen reloads its
   catalogue when it moves. **The cart is deliberately untouched** — lines
   already rung up keep the price the customer was quoted.

   The till's own admin screens turn **read-only** for products, prices and
   categories whenever a backend is configured, with a banner pointing at the
   console. Barcode assignment stays enabled — it has to, since the till owns
   codes — and so does **Entradas de mercancía**, the scan-in screen for
   deliveries. Only *generation* of internal codes and the label sheet are
   disabled there.

   Verified end to end: price changed in the console → till had it within one
   cycle, local revision advanced, `/api/devices` served the new number.

4. ✅ **Users — done 2026-08-24.** Central owns cashier and admin accounts and
   pushes them down with the catalogue. PIN hashes are produced in the
   register's own scrypt format, so the till verifies them directly without
   re-hashing anything — confirmed by hashing a PIN centrally and verifying it
   with the till's `auth.verify_pin`.

   `failed_attempts` and `locked_until` deliberately stay register-side: lockout
   is runtime state belonging to the machine where the PIN was typed, and
   pushing it down would either clear a lockout that is actively protecting the
   drawer or apply one register's failures to another.

   Guards: PIN length is enforced by role (4 cashier / 6 admin, matching the
   till), and the console **refuses to remove the last active admin** — a
   register with no admin cannot authorise an override or close a shift with a
   shortfall, and nobody can fix that from the shop floor.

5. **Receiving is built but unused.** Every product currently reads
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
