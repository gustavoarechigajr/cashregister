---
tags: [cashregister, plan, architecture]
---

# Cash Register — Project Plan

**Site:** Store building (`STR`) — confirmed 2026-08-23. Currently staged on
`HSE-House-SW-4 Te1/0/45`; final home is `STR-Store-SW-6`.
**Hardware:** Lenovo ThinkCentre `10MRS02D00` — i5-6400T (4c/4t), **16 GB RAM**, 500 GB NVMe SSD
(+ empty 500 GB SATA HDD). Currently Windows 11 Pro build 22000. Verified over SSH 2026-08-23.
**Scope as of 2026-08-23:** cash only · no CFDI · store merchandise

## Scope decisions

| Question | Answer | Consequence |
|---|---|---|
| Facturas / CFDI 4.0 | **No** — cash tickets only | No PAC integration. Removes the largest compliance surface. Revisit if the business ever needs to invoice. |
| Payments | **Cash only** | No processor SDK, no PCI surface. But: needs a cash drawer, shift open/close and end-of-day reconciliation. |
| Sells | **Store merchandise** | Classic retail inventory. SKU → stock count → reorder. The case where remote tracking pays off most. |

Because tax and card processing are both out, **building this custom is a reasonable
call** rather than a risky one. What's left is a catalog, a cart, and a stock count.

## ⚠️ The business is seasonal — this is the real constraint

Recovered from three years of real sales in the existing Aronium database:

| Month | Sales | Total |
|---|---|---|
| 2023-04 | **825** | $56,372 |
| 2024-03 | 355 | $22,441 |
| 2025-04 | 292 | $20,102 |
| 2026-03 | 153 | $13,181 |

Busiest single days: **2023-04-07 (149 sales)**, 2023-04-08 (112), 2024-03-30 (98).

Essentially all revenue lands in **late March / early April — Semana Santa.** The rest of
the year is close to idle. That has four consequences that outrank everything else:

1. **The deadline is Semana Santa 2027.** Easter Sunday is **28 March 2027** — about
   seven months out. Miss it and the next opportunity is a full year later.
2. **Do not cut over cold.** A hand-built POS whose first real test is the single
   highest-revenue week of the year is an unacceptable risk. See fallback below.
3. **Seasonal staff.** Whoever runs the till may be untrained and new each year. The UI
   has to be obvious without a manual — this raises the value of a category button grid
   over a search box.
4. **Peak throughput ~150 sales/day**, bursty. Fast, but not a hard engineering problem.

### Fallback: dual-boot, do NOT wipe Windows

The machine has an **empty 500 GB SATA HDD** alongside the 500 GB NVMe running Windows.
Install Debian beside Windows and leave the Aronium install completely intact.

If the new system isn't ready by March, you reboot into Windows and run the season on
Aronium exactly as you did in 2023, 2024, 2025 and 2026. That converts a
bet-the-season gamble into a no-risk experiment, using hardware already in the box.

## The one architectural rule: offline-first

The register keeps a **local database and works with the network completely down.**
Not a browser pointed at a server.

This is not paranoia, it's a read of this specific network's history:
- TrueNAS was down ~2 months (2026-05-18 → 07-28)
- `STR-Store-SW-6` flaps because it is **losing power**, diagnosed 2026-08-18 — and
  that is the switch the store register sits behind
- The store has no alternate network path (`POWER-INFRASTRUCTURE-PLAN` §"cycle only")

A register that stops selling during a network outage converts a network problem into
a closed business. Non-negotiable.

### Sync model — sales are events, not state

The design detail that makes offline-first easy:

> **Never sync "stock level" in both directions.** That is conflict hell.
> The register emits **immutable sale events**. Central derives stock from
> `received − sold`. Events only ever move one way and can never conflict.

```
Register  ──  sale events (append-only, via local outbox)  ──▶  Central
Register  ◀──  catalog: SKUs, names, prices                 ──   Central
```

If the link is down the outbox just grows and drains later. Nothing is lost, nothing
needs merging, and a sale is never blocked on the network.

Stock counts on the register are a **cached hint** for the cashier, not the truth.
Truth is central. This is correct anyway — a single register can't know about a
shipment someone received this morning.

## Components

> **Revised 2026-08-23.** An earlier draft put the backend in `trz-docker-14` and built
> it first. Both are now reversed — see "Deployment" and "Phasing" below.

Two deployables, split by responsibility.

| | **Front-end (register)** | **Back-end (server)** |
|---|---|---|
| Runs on | ThinkCentre at the store | `trz-docker-14`, or hosted |
| Shows current purchase, totals | ✅ | |
| Price catalogue at the till | ✅ (local copy) | source of truth |
| Take cash, print receipt, kick drawer | ✅ | |
| Shift open/close, reconciliation | ✅ | reporting |
| Inventory tracking, receiving | | ✅ |
| Create/edit products, set prices | **✅ (admin only)** | ✅ |
| Assign & generate barcodes | **✅ (admin only)** | ✅ |
| Printable barcode label sheets | | ✅ |
| Users, roles, reports | | ✅ |

> ⚠️ **The front-end is not a thin client.** It holds its own copy of the catalogue and
> writes sales to a local database first. "Displays the catalogue" must mean *its local
> copy*, or a network outage closes the store. See offline-first above.

### 1. Register (the till)

- **OS: Debian 13.** Matches the rest of the fleet (`10.0.0.11`, `10.0.0.254`,
  `trz-docker-14` are all Debian 13) — same patching, same muscle memory.
- **Kiosk, not a desktop.** Minimal install + `cage` (or X + Chromium `--kiosk`),
  autologin, no DE. Boots straight into the till. A cashier should not be able to
  reach a file manager.
- **Local DB: SQLite.** Single file, zero admin, trivially backed up, genuinely
  bulletproof for one register. Postgres here would be overhead with no payoff.
- **App: Python + FastAPI, UI served on `localhost` and rendered in the kiosk browser.**
  Same skills as the central UI, touch-friendly, styleable, and developable from Gus's
  PC. No network dependency — it's all loopback.

### 2. Central service

- **Its own LXC on `trz-proxmox-13`** — *not* inside `trz-docker-14`.
- **Postgres**, in that container.
- Owns: catalog, cost prices, receiving/restock, sales history, reporting, low-stock alerts.
- **LAN-only. No Cloudflare Tunnel.** Inventory is managed from the home network, so the
  service never needs to be internet-reachable. An earlier draft proposed exposing it
  through the existing tunnel — dropped, because it would put four years of sales data
  behind a public hostname for no benefit. If off-site access is ever needed, use the
  existing Tailscale tailnet, not a public tunnel.

## Deployment

### The register is the product; the backend is a satellite

The register must work fully offline, which means it already needs the complete
catalogue, sales model, receipts, shifts and cash handling **locally**. The backend
therefore adds nothing the register does not independently require — it adds remote
admin, reporting and backup.

So the register is built to run the store with the backend switched off, unplugged, or
never built at all. If March arrives with the backend half-finished, the store opens.

This also quarantines the riskiest part of the project. **Sync is harder than the till
UI and harder than the printer.** Keeping it additive means a sync bug degrades to
"reports are stale", never "the store cannot sell".

### Admin catalog screens run locally too — decided 2026-08-24

Extends the same offline-first reasoning from sales to catalog editing.
`STR-Store-SW-6` has an unresolved power fault and no redundant path (see
"Network placement" below) — if the backend is unreachable and a price needs
fixing or a new product needs a barcode assigned mid-shift, waiting for the
network is not an acceptable answer any more than it would be for a sale.

So the admin screens (product/price edit, barcode assignment, label
printing — the Inventario and Códigos de barras views prototyped in
`design/`) are **served by the same FastAPI app as the till, reading and
writing the same SQLite database** — not a separate app that depends on the
backend being up. Gated to `role == 'admin'` on the existing PIN session;
reached from an entry point the cashier login never sees.

The hosted backend (Phase 5) manages the *same* database remotely when it
exists. That makes it the convenient way in, day to day — not the only way
in. A price fix at the counter with the internet down still works.

This also means **the earlier "admin password belongs on the central
service, not the sell screen" note is superseded**: the till's own PIN
login is what gates local admin access too, not a separate credential.

### Why not `trz-docker-14`

That container is a **privileged LXC** running Plex, the *arr stack, qBittorrent, Home
Assistant, Frigate and Cloudflared — a media stack with a different change cadence and a
different uptime expectation from a system that handles money. Restarting it to fix
Sonarr must not be able to touch the point of sale. Precedent: the TrueNAS outage took
Plex, Frigate, qBittorrent and Portainer down together for two months.

A dedicated LXC on `trz-proxmox-13` needs ~1 GB RAM and ~8 GB disk, gets independent
snapshots and restarts, and sits at Terraza on good power and the core switch rather
than behind the store's flapping one.

| | Register | Backend |
|---|---|---|
| Where | ThinkCentre, Store | Own LXC on `trz-proxmox-13` |
| Store | SQLite — authoritative for its own sales | Postgres — authoritative for the catalogue |
| Runs under | **systemd + venv, not Docker** | Docker or systemd |
| Listens on | localhost only | LAN only, no tunnel |
| If the other is down | sells normally | shows stale sales |

**No Docker on the register.** On a kiosk it is a daemon that can fail between the
cashier and the till, it slows boot, and it is another layer to debug in a shop at 2 pm
during Semana Santa. A systemd unit and a virtualenv start faster and fail more legibly.

### Build for the split from day one

The register ships before the backend exists, but its data model must not have to change
when the backend arrives:

- **Sale ids are UUIDs, generated on the register** — never autoincrement integers, or
  ids collide the moment a second register or the server assigns any.
- **Every sale carries a `register_id`**, even with one register.
- **A `sync_outbox` table exists from the first migration**, written to from day one and
  simply never drained until the backend exists.
- **All timestamps are UTC with an explicit offset.** The store is `America/Mexico_City`;
  display converts, storage does not.
- **The catalogue carries a monotonic `revision`** so the backend can later serve deltas
  rather than full dumps.
- **The domain model lives in shared code**, imported by both sides, so the schema cannot
  drift between them.

### Backups matter more than the backend

The register's SQLite file *is* the business. Nightly dump to the NAS, plus a copy to the
empty 500 GB HDD already in the machine. Hardware death is a likelier failure than
anything architectural, and it is the one the backend does not protect against.

## Users and roles

### What exists today (and why it needs changing)

From the recovered database:

| User | Level | Sales rung up |
|---|---|---|
| `Admin` | 9 | 3 |
| `Tienda Balneario` | 0 | **1,817** |

**One shared cashier account rang up 99.8% of all sales.** Nothing in four years of
history is attributable to a person.

Worse, the permission split is inverted for a cash business. `Level 0` — the shared
account — holds `Order.Void`, `Order.Item.Void`, `Refund`, `Payment.Discount`,
`Invoices.Delete`, `CashDrawer.Open`, `Management.Products` and
`Management.Stock.ShowCostPrices`. `Level 9` is almost entirely configuration screens
(TaxRates, Company, Countries, FloorPlans) that nobody touches during a season.

So the locked-down things are settings, while voids, refunds, discounts and
**opening the drawer without a sale** are wide open on an untraceable login.

### Target model — two roles

| | **Admin** | **Cashier** |
|---|---|---|
| Ring up sales, take cash, print | ✅ | ✅ |
| Close own shift | ✅ | ✅ |
| Add / edit / remove products | ✅ | ❌ |
| Receive stock, set cost prices | ✅ | ❌ |
| See cost prices and margins | ✅ | ❌ |
| Void / refund a completed sale | ✅ | ⚠️ override |
| Discount | ✅ | ⚠️ override |
| **Open drawer with no sale** | ✅ | ⚠️ override |
| **Retiro parcial** (cash drop) | ✅ | ⚠️ override |
| Reports, sales history | ✅ | own shift only |
| Manage users | ✅ | ❌ |

**Per-person cashier accounts, never a shared one.** This is the single most important
change. Without attribution, shift reconciliation cannot tell you anything — a short
drawer just means "someone".

**PIN login at the till, not passwords.** Four-digit PIN, fast on a touchscreen. An
admin's own six-digit till PIN is also what gates the local admin screens (see
"Admin catalog screens run locally too") — no separate password login on the register.

**Manager override, not a second login.** For voids, refunds, discounts and no-sale
drawer opens the cashier stays logged in and an admin enters an override PIN. Faster
than switching users, and it records *both* people on the event.

**Every drawer open is a logged event** — sale or no-sale, with who, when and why.
In a cash-only business that log plus per-shift counts *is* the loss prevention.

### Offline auth — a real constraint

The till must authenticate with the network down, so cashier PIN hashes sync down from
central and are verified locally. Consequences to design for honestly:

- A 4-digit PIN is only 10,000 possibilities. Hashing slows an attacker but does not fix
  that. **Rate-limit and lock out after a few failures**, and treat the till database as
  sensitive at rest.
- Give the **admin override PIN 6+ digits** — it is the higher-value secret.
- The realistic threat is someone at the counter, not an attacker with a stolen disk.
  Size the controls to that, but don't pretend the PIN is strong.

### 3. Peripherals — all already owned and **verified on Linux**

Tested 2026-08-23 on Gus's PC (Linux Mint 22.2, kernel 6.17) with the real hardware —
not inferred from Windows. `usblp` and USB-HID are stable across Mint and Debian 13,
so these results carry over. **Nothing needs buying except a UPS.**

| Item | Notes |
|---|---|
| **Receipt printer** | `POS-58` thermal, USB `0483:070b`. ✅ **Verified:** `usblp` binds with no vendor driver and exposes `/dev/usb/lpN` (`root:lp`, `0660`); raw ESC/POS accepted. **32 columns** confirmed empirically by where text wrapped. |
| **Cash drawer** | ✅ **Verified:** opens from Linux via `ESC p 0 25 250` (`1b 70 00 19 fa`) down the printer's RJ11. |
| **Barcode scanner** | Tera 5100, 1D laser, wireless dongle **`0581:011c`** — named in the USB ID database. ✅ **Verified:** binds as a plain HID keyboard (`/dev/input/by-id/usb-0581_011c-event-kbd`), emits valid EAN-13 with a trailing Enter. ⚠️ **1D only — no QR codes.** |
| **UPS** | **Required.** Confirmed ThinkCentre — no battery. Store switch already has a documented power fault (2026-08-18). This is the one piece of hardware that genuinely matters. |

## Network placement — the Store switch

The register lives behind **`STR-Store-SW-6` (`10.0.0.6`)**, a C9200CX-12P-2X2G.
Ports `Gi1/0/2`–`Gi1/0/12` are free (`notconnect`); only `Gi1/0/1` is used, by the
store AP. Config needed on whichever port is chosen:

```
interface GigabitEthernet1/0/2
 description CASHREGISTER-THINKCENTRE-10.0.0.22
 switchport mode access
 switchport access vlan 10
 spanning-tree portfast
```

⚠️ **The free ports sit in VLAN 1, not the blackhole VLAN 999 used elsewhere** — and
VLAN 1 has no addressing here, so a device plugged in without config gets nothing.

⚠️ **Verify VLAN 10 is permitted on the `Te1/1/3` uplink to the House switch** before
assuming it works. This network's trunks use *explicit* allowed-lists — `ap-onboarding`
shows `switchport trunk allowed vlan 20,30,40,50,60`, with VLAN 10 absent. The switch's
own management on VLAN 10 is reachable, so something permits it, but confirm rather than
assume; an omitted VLAN fails silently at the uplink.

### ⚠️ Wi-Fi option — no cable run to the store register yet

**The ThinkCentre has no wireless hardware.** Verified 2026-08-23: the only adapter is
an `Intel Ethernet Connection (2) I219-V`. Wi-Fi requires buying a USB adapter, and that
has consequences:

**Buy a MediaTek, not a Realtek.** Cheap USB Wi-Fi is the single worst-supported
category of Linux hardware. Realtek USB parts (RTL8811AU/8821CU/8188…) usually need
out-of-tree DKMS drivers that break on kernel updates — a register that stops reaching
the network after an unattended `apt upgrade` is exactly the failure to avoid.
Pick a chipset with a **mainline in-kernel driver**:

- **MediaTek MT7921AU** (`mt76` driver) — dual-band, current best choice
- **MediaTek MT7612U** (`mt76x2u`) — older, dual-band, well supported

**It must do 5 GHz.** Two reasons, and the second is easy to miss:

1. During Semana Santa the store AP is saturated with guest phones — 2.4 GHz will be
   unusable at exactly the busiest moment.
2. **The barcode scanner's dongle is proprietary 2.4 GHz.** Putting the register's Wi-Fi
   on the same band means the till competes with its own scanner for spectrum.

**Wi-Fi changes the VLAN and the IP.** No SSID maps to VLAN 10 — the wireless VLANs are
20 (Surveillance), 40 (IoT), 50 (Users) and 60 (Guest). On Wi-Fi the register joins
`CasaVistaHermosa` → **VLAN 50**, so the planned static `10.0.0.22` does not apply; use
a DHCP reservation on `10.0.50.x` instead.

> Worth noting: this accidentally achieves the segmentation originally recommended —
> Wi-Fi cannot put the register on the MGMT trust boundary even if you wanted it there.

**Recommendation: run the cable before March.** The machine has gigabit ethernet built
in and the store switch has eleven free ports. One cable run removes an adapter purchase,
the driver-stability risk, RF contention with the scanner, and AP congestion at peak.
Wi-Fi is workable — the offline-first design absorbs it — but it is strictly worse for
the one week that matters.

### This is why offline-first is load-bearing, not cautious

Two facts about this specific site:

1. **`STR-Store-SW-6` is still rebooting itself.** The 2026-08-14 snapshot caught it at
   **12 h 21 m uptime** — consistent with the power fault diagnosed 2026-08-18 in
   `ALEX-HANDOFF` §3c ("it is losing POWER, not link"). Unresolved as of this writing.
2. **The store has no redundant path.** `Te1/1/3` to the House is the only live uplink;
   `Te1/1/4` to the Entrance reads `notconnect`. One fibre, no failover.

So during the one week that generates the year's revenue, the register's network will
sit behind a switch with a known power fault and no second path. The register must sell
through that. It also means:

- **The UPS must cover the register *and* the printer** — a receipt half-printed through
  a power blip is a sale in dispute.
- **Fixing the store switch's power feed is a prerequisite**, tracked in the Networking
  repo, not here — but it belongs on the pre-Semana-Santa checklist.

## Barcode generation

Many items will never have a manufacturer code — the top seller, `Vasos Individual`
($2), is loose cups. An admin assigns codes for these and produces a printable sheet
to stick on the shelf or the item — from the backend when it's reachable, or from the
register's own admin screen when it isn't (see "Admin catalog screens run locally too").

### Use the GS1 in-store range — this matters

**Generate EAN-13 codes beginning with prefix `2`.** GS1 reserves prefixes **20–29**
for in-store / restricted circulation exactly this purpose.

Inventing codes outside that range risks **colliding with a real manufacturer's
barcode** — scan a supplier's product later and it rings up as your loose cups. The
prefix-2 range is guaranteed never to be issued to a manufacturer, so collisions are
impossible by construction.

- Compute the **mod-10 check digit** properly, or the scanner rejects the code.
- Flag internally-generated codes in the DB so they're distinguishable from scanned ones.
- The existing `Barcode` table already allows multiple codes per product (230 codes /
  211 products) — keep that; an item can have both a supplier code and an internal one.
- EAN-13 is read natively by the Tera 5100 laser. Code 128 is an alternative but is
  less universally enabled by default on cheap scanners.

### Receipt encoding — settled by test

- **Use CP858** (`ESC t 19`). CP437/850/858/1252 all rendered `Ñoño áéíóú ¿Cuánto?`
  correctly. CP1252 uses different byte values from the others and still came out right,
  which proves the printer honours `ESC t` rather than ignoring it. Spanish is a non-issue.
- **32 columns normally — but 16 in double-width.** `GS ! 0x11` halves usable columns.
  Lay double-width lines out at 16 columns; never build a 32-column row and truncate it,
  or the amount is what gets cut. (Found on the first test print: `TOTAL` printed,
  `$97.00` did not.)

### Scanner behaviour — settled by test

It is a **keyboard wedge**: types into whatever has focus, then presses Enter.

- A scan is self-terminating — read to the Enter, no keystroke-burst timing needed.
- But it types into *any* focused control. The till must capture scans **globally**
  rather than relying on a focused text box, or a scan lands in whatever field the
  cashier last touched. (Demonstrated live: a test scan typed itself into the chat.)

### Printing the labels

- Output a **PDF sheet** sized for whatever label stock is used.
- ⚠️ **Print at adequate magnification.** EAN-13 at 100% is ~37 × 26 mm. Shrink it too
  far and a laser scanner stops reading — the most common failure with home-made labels.
  Verify by scanning a printed sheet before making hundreds.
- Alternative for one-offs: the POS-58 can print barcodes directly via the ESC/POS
  `GS k` command. Fast, but thermal paper fades and isn't adhesive — fine for a
  temporary shelf tag, not for long-lived labels.

## Phasing

Ordered so each phase is useful on its own.

Ordered so the store can open at any point after Phase 3.

- **Phase 0 — Reimage.** ✅ Hardware verified on Linux 2026-08-23. Aronium data backed up
  to `backup-from-windows/`. Debian 13 netinst, no desktop, SSH server only.
- **Phase 1 — Catalogue data.** ✅ Done 2026-08-23: `tools/import_aronium.py` and
  `tools/categorize.py` produce a cleaned 206-product catalogue in 12 till categories.
- **Phase 2 — Register: schema + sell flow.** ✅ Running on the ThinkCentre 2026-08-23.
  SQLite, cart, cash tender, change due, per-cashier PIN with lockout, shift open,
  global scan capture, price check. Standalone; no server anywhere. 12/12 smoke tests
  pass on the real hardware. Still to do here: shift close, retiro, refunds/voids.
- **Phase 3 — Register: printer, drawer, receipts.** 🔨 **Mostly done 2026-08-24, and
  verified on the real hardware:**
  - ESC/POS receipts print on every sale. **CP437, not CP858 as planned** — CP437 already
    carries `ñ` and the accented vowels, which is all this shop needs; CP858 only adds the
    euro sign. Verified `peñafiel 2lt` encodes to `0xA4`.
  - Drawer kick works and is audited with *why*: `sale` ×3, `shift_close` ×2, `retiro` ×2
    in the live audit log.
  - Reprint of the last ticket, stamped `*** COPIA ***` (macropad F17).
  - Corte de caja printed at shift close, listing each retiro with its envelope number.
  - Barcode label sheets print real bars (inline SVG) — **scan-tested against the Tera
    5100 by Gus and confirmed working.**
  - Test mode (admin-only) suppresses printing and the drawer for training.
  - Receipts confirmed printing in practice: real sales with `test_mode` off and **zero
    `receipt_failed` audit rows**.

  **Refunds: handled manually, by decision (Gus, 2026-08-24).** There is no refund flow
  and there does not need to be one: the cashier opens the drawer and hands the cash
  back. The drawer opening is audited, which is the accountability that matters in a
  cash-only shop this size. Phase 3 is therefore **complete for trading purposes**.

  Two consequences to be aware of rather than fix:
  - The corte will show a **faltante** equal to the refund, because `v_shift_expected`
    is float + sales − movements and nothing records the cash leaving. Over
    **$50** (`SHORTFALL_REQUIRES_ADMIN_CENTS`) that blocks the close until an admin
    PIN is entered; under it, the difference is silently attributed to the cashier.
  - The item stays counted as sold, so once stock exists it will read low by one.

  If either becomes annoying, the cheap remedy is already in the schema:
  `cash_movement.kind = 'payout'` is accepted and **already subtracted by
  `v_shift_expected`**, so recording a refund as a payout — the retiro dialogue with a
  different kind — makes the corte balance without building a refund system.
- **Phase 4 — Kiosk hardening + backups.** Autologin, no desktop, systemd unit, nightly
  SQLite dump to the NAS and the spare HDD.
- **Phase 5 — Backend.** LXC on `trz-proxmox-13`, Postgres, receiving, reporting. Drains
  the outbox the register has been filling since Phase 2. 🔨 **Started 2026-08-24:**
  container `116 / trz-caja-16` at `10.0.0.16` is up (Debian 13.6, unprivileged,
  `onboot`), PostgreSQL 17.11 installed, database `caja` created with the full schema
  (11 tables + 2 reporting views) owned by the `caja` role. ✅ **Working end to end
  2026-08-24:** idempotent ingest API, a register-side client draining every 30 s
  (52 queued rows drained to 0), and a read-only reporting UI at
  `http://10.0.0.16:8090/` — summary, ventas with ticket lines, turnos y cortes,
  most-sold products. **Still to build: catalogue push and receiving** (receiving
  depends on it). See `backend/README.md`.
- **Phase 6 — Admin catalog screens: product/price edit, barcode generation, printable
  label sheets.** ✅ Local version done 2026-08-24: `/admin` served from the same
  FastAPI app and SQLite DB as the till, gated by `require_admin_session()` on the
  existing PIN session — see "Admin catalog screens run locally too". Verified live
  on the ThinkCentre: generated a real internal EAN-13 for a product missing one
  (continues the existing 2303311xxxxx series), edited and saved a product's cost,
  margin recalculated correctly. Still to do: the backend-hosted version, once
  Phase 5 exists.
- **Phase 7 — Low-stock alerts, dashboards, refinement.**

## The lag is not a hardware problem

Measured 2026-08-23 over SSH: i5-6400T, 16 GB RAM, NVMe SSD, 380 GB free. Top eight
processes together used **~1.2 GB of 16 GB**. Nothing is starved.

Two likely explanations, in order:

1. **Parsec was running** (`parsecd`). If responsiveness was judged over a Parsec remote
   session, that is streaming latency, not machine latency. Test at the physical console
   before concluding anything.
2. **Windows 11 build 22000 = 21H2, which went end-of-support in October 2023** — roughly
   three years with no security updates. And the i5-6400T is 6th-gen Skylake, which Win11
   does not officially support (8th-gen minimum), so this is a bypassed install.

The EOL point is the real one. A machine handling cash should not be running an OS that
stopped receiving security patches three years ago. **That is the argument for Debian —
not the speed.** The speed is a bonus, and this hardware will run a Debian kiosk with
room to spare.

## What will actually be hard

Not the selling screen. That's the easy part and it's tempting to polish it first.
The parts that bite in real operation:

- **Shift close / cash reconciliation** — counted drawer vs expected. This is how you
  find out about mistakes and theft. Design it in Phase 2, not later.
- **Retiro parcial (cash drop) is a separate operation from the corte.** On a busy
  Semana Santa day the drawer accumulates more cash than anyone wants sitting in it, so
  cash gets moved to the safe *mid-shift* — without closing the turn. Two consequences:
  - The arithmetic changes: `esperado = fondo + ventas en efectivo − retiros`. A corte
    that ignores drops will read as a huge shortfall.
  - **The cashier counts different things in each mode** — the cash being *removed*
    during a retiro, everything in the drawer during a corte. Counting the wrong one is
    the easy mistake; the UI has to make the two visually unmistakable.
  - Each drop records amount, time, envelope number, who counted and who authorized.
    Requires an admin PIN — it is cash physically leaving the drawer.
- **Refunds, voids, and corrections** — a cashier will ring up the wrong thing on day
  one. If a sale event is immutable, a refund must be its own compensating event,
  never an edit or a delete.
- **Miscounts and shrinkage** — real stock drifts from computed stock. Needs a
  stock-take flow that reconciles them and records the adjustment.
- **Barcode gaps** — some merchandise won't have a scannable code. Needs a fast
  search-and-tap fallback and a way to assign your own SKUs.

## Open questions

1. ~~ThinkPad or ThinkCentre~~ — **answered: ThinkCentre.** Needs a UPS and a monitor.
2. ~~Actual specs~~ — **answered: no upgrade needed.** The hardware is not the bottleneck; see below.
3. ~~Where does the register live~~ — **answered: the Store.** See "Network placement".
4. **Does anyone else need access** to inventory remotely, and from where?
5. ~~How many SKUs~~ — **answered: 211 products, 34 groups, 230 barcodes.** Hundreds, not
   thousands. Category button grid + barcode scan, with search as the fallback. Note some
   items (loose cups, ice) will never have a scannable code.
6. **Is a rebuild worth it before March?** Aronium works and has three seasons of history.
   The honest case for replacing it is Windows 11 being out of support, Linux, and remote
   inventory — not that the current till is bad. Dual-boot means you don't have to decide
   today.

## Related

[[TODO]] (running backlog) · `backend/README.md` (Phase 5) · [[SSH-SETUP]] · Networking repo: [[network-topology]] · [[POWER-INFRASTRUCTURE-PLAN]]
