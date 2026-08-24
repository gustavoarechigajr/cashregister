---
tags: [cashregister, backend, phase5]
---

# Central service — `trz-caja-16`

Phase 5 of [[PLAN]]. **Started 2026-08-24. Container, database and the sync
ingest API all exist and are verified. The register-side client does not.**

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
| API | `caja-api.service` → `uvicorn` on **`0.0.0.0:8090`**, bearer token in `/etc/caja/env` (mode 640, `root:caja`) |

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
2. **Register-side sync client** — a small loop in the till that drains
   `sync_outbox` (35+ rows are already queued, dating from Phase 2) whenever
   the backend is reachable, and does nothing at all when it is not. Must never
   block a sale.
3. **Catalogue push** — central becomes the owner; the register receives. Until
   this exists the till's local admin screens remain the source of truth, so
   do not import the catalogue centrally yet or there will be two masters.
4. **Receiving + reporting UI** — the actual point of the backend.

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
