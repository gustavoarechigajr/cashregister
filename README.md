# Cash Register

Point-of-sale and inventory system for the store at **Balneario Vista Hermosa**.

Replaces an Aronium (Windows) install that ran the store from 2023 to 2026. The
register hardware is being reimaged from Windows 11 to Debian.

## Architecture

Two components:

**Front-end — the register.** Runs on the ThinkCentre at the store. Shows the current
purchase, running total, and price catalogue; takes cash, prints the receipt, kicks the
cash drawer, and handles shift open/close.

**Back-end — the server.** Inventory tracking, product and price management, barcode
assignment and generation, printable label sheets, users, and reporting.

> **The front-end is not a thin client.** It keeps a local copy of the catalogue and
> writes every sale to a local database first, syncing to the back-end when it can.
> The store must be able to sell with the network down.

### Sync model

Sales are **immutable events**, never synced state:

```
Register  ──  sales, shifts, cash, audit, receiving  ──▶  Back-end
Register  ──  barcodes (the scanner lives here)      ──▶  Back-end
Register  ◀──  products, prices, costs, categories, users  ──  Back-end
```

**Ownership follows the hardware.** Barcodes flow *upward*: every code is assigned by
scanning it onto a product, and the scanner is at the till. Prices flow *downward*,
because that is where someone sits with a price list. No field is written from both
ends — see [`docs/INVARIANTS.md`](docs/INVARIANTS.md).

Stock is derived centrally as `received − sold`. Nothing ever needs merging, and a sale
is never blocked on the network.

## Scope

Cash only · no CFDI/facturas · retail merchandise · single register.

## Hardware

| | |
|---|---|
| Register | Lenovo ThinkCentre `10MRS02D00` — i5-6400T, 16 GB RAM, 500 GB NVMe |
| Receipt printer | POS-58 thermal, 58 mm, USB `0483:070B` — ESC/POS |
| Cash drawer | RJ11, kicked by the printer |
| Scanner | Tera 5100, 1D laser, 2.4 GHz dongle. Presents **two** USB ids: `0581:011C` (matched by `devices.py`, drives the *Escáner: Listo* pill) and `0E8F:00A8` (`DaKai 2.4G RX`). Extra ids can be added without a code change via `CASHREGISTER_SCANNER_IDS`. |

## ⚠️ Seasonality

Nearly all revenue lands in one week — **Semana Santa**. Peak day on record is
2023-04-07 with 149 sales. The rest of the year is close to idle.

**Target: Easter 2027 (28 March).** There is no second chance in a given year.

## Status

**Live.** The register is at the store and selling; Caja Central runs on `10.0.0.16`
and owns the catalogue. Remaining work is tracked in [`TODO.md`](TODO.md).

See [`PLAN.md`](PLAN.md) for the design and its reasoning, and
[`docs/INVARIANTS.md`](docs/INVARIANTS.md) for the rules any new feature has to
respect — read that one before touching sync, barcodes, stock or history.

## Repository layout

| Path | Contents |
|---|---|
| `PLAN.md` | Architecture, scope decisions, roles, phasing |
| `TODO.md` | Current state and what is still open |
| `docs/INVARIANTS.md` | **Rules every new feature must respect** |
| `docs/data-quality.md` | Known catalogue data problems |
| `docs/archive/` | Superseded docs, kept for history |
| `register/` | The till: FastAPI app, kiosk provisioning, schema |
| `backend/` | Caja Central: FastAPI app, Postgres schema, console UI |
| `tools/` | `deploy.sh`, `shot.sh`, `check-barcode-sync.sh`, importers |

## Data recovered from the old system

The Aronium SQLite database and product exports were recovered before the reimage:
211 products, 34 groups, 230 barcodes, 1,820 sales, 3,052 line items.

**Not in this repository.** It contains sales history, customer records, and user
password hashes — it is `.gitignore`d and belongs on the NAS, not on GitHub.
