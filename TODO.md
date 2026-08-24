---
tags: [cashregister, todo, backlog]
---

# Cash Register — Backlog

Found while actually using the register, **2026-08-24**. Ordered by my read of
priority, not by the order they were reported. See [[PLAN]] for the phase plan;
this is the running list of work that falls outside it.

> **Deadline context:** Semana Santa 2027 (Easter **28 March 2027**). Items 2 and 3
> below are *data and label correctness* problems — they get worse the longer they
> run, because every product entered meanwhile inherits the fault.

---

## 1. ✅ Kiosk keyboard layout — FIXED 2026-08-24

The kiosk ran a **US** layout. The OS was configured correctly (`localectl` →
`latam`, `/etc/default/keyboard` → `XKBLAYOUT="latam"`), but the `cage` unit set no
`XKB_DEFAULT_LAYOUT`, and `xkbcommon` — what `cage`/wlroots use — does **not** read
`/etc/default/keyboard` (that file is for the console and Xorg). With the variable
unset it silently falls back to `us`. Nothing in `register/provision/` ever set it,
so this was a gap from first install, not config drift.

**Fixed** by adding to `cashregister-kiosk.service` (repo source updated too, so a
rebuild can't regress it):

```
Environment=XKB_DEFAULT_LAYOUT=latam
Environment=XKB_DEFAULT_MODEL=pc105
```

Verified present in the running compositor's process environment after restart.
⏳ **Still needs a physical confirmation** — type `ñ` and `á` at the till.

### ⚠️ Correction: the mangled names came from Aronium, not from this bug

My first read blamed the layout for corrupting the catalogue. **That was wrong**, and
the distinction matters for how much of the catalogue to touch. The original Windows
`backup-from-windows/pos.db` already contains:

```
pe;afiel 2lt
sandalia de ni;o
```

So the `;`-for-`ñ` substitution happened on the **old Windows/Aronium system** and was
inherited by the import. Only **6** products have ever been typed on the register at
all (diffed against the Aronium export). One of them, `sandalia de nio`, is the
imported `sandalia de ni;o` with the `;` hand-deleted — someone tried to fix it and
couldn't type `ñ`, which is good circumstantial evidence for the layout gap but is not
the same as the bug having created the damage.

**Repaired (2026-08-24), mirroring what `db.update_product()` does — name +
`updated_at`, and `catalogue_revision` bumped 8 → 9. DB backed up first to
`register.db.bak-20260824-120657`:**

| id | was | now |
|---|---|---|
| 171 | `pe;afiel 2lt` | `peñafiel 2lt` |
| 208 | `sandalia de nio` | `sandalia de niño` |

### 🟡 Open decision: the catalogue has no accents at all

**Zero** of the 207 product names carry an accent or `ñ` — and neither do any of the
211 in the Aronium original. This is inherited data-entry style, not corruption, so I
have **not** touched it: rewriting ~34 of the owner's product names is a call for Gus,
not a repair. Fixing only `Papel Higienico` while leaving thirty others would be worse
than leaving all of them.

Candidates if you want the sweep (brand stylings like `Arcoiris`, `Pikaros`,
`trikitrakes` deliberately excluded as ambiguous):

`Atun`→`Atún` (7) · `Sodico`→`Sódico` (10, 56) · `Azucar`→`Azúcar` (21, 104) ·
`Nectar`→`Néctar` (27, 203) · `Limon`→`Limón` (29, 46, 64, 103, 114) ·
`Incognita`→`Incógnita` (32) · `Jabon`→`Jabón` (48, 169) ·
`La Costena`→`La Costeña` (50–53) · `Panales`→`Pañales` (60) ·
`Higienico`→`Higiénico` (61) · `Jalapeno`→`Jalapeño` (67, 101, 164) ·
`Clasicas`/`Clasico`→`Clásicas`/`Clásico` (73, 86, 119, 123) · `Tajin`→`Tajín` (86) ·
`Maria`→`María` (88) · `Medica`→`Médica` (90) · `Te`→`Té` (114) ·
`Vehiculos`→`Vehículos` (145) · `Carbon`→`Carbón` (157) · `carton`→`cartón` (162) ·
`chicharos`→`chícharos` (165) · `pinguinos`→`pingüinos` (184) ·
`acuatica`→`acuática` (190) · `Chicharron`→`Chicharrón` (200) · `cafe`→`café` (206)

**Separate data-quality typos spotted while auditing** (not accents, worth a decision
too): `Floretinas Clasicas` (123) vs `Florentinas Cajeta` (43) — one is misspelled;
`Sprite 2.Lt` (84); `Bolzaza Flemin hot` (195) — probably `Flamin`.

---

## 1b. ✅ Focus survives re-renders everywhere — FIXED 2026-08-24

Pressing Enter on a category unfocused the strip and killed the arrow keys. Root
cause is the same trap as the roving-listener bug: `renderCats()` wipes and rebuilds
`#cats`, destroying the very button the user is standing on, so focus fell to
`<body>` — and the arrow listener lives on `#cats` and only fires while focus is
inside it.

Rather than patch that one site, added a shared **`keepFocus(box, rebuild, keyOf)`**
helper and routed every unhandled rebuild through it. It restores by key where rows
carry a stable id, otherwise by position, so a row that disappears hands focus to its
neighbour instead of nowhere.

| file | function | restored by |
|---|---|---|
| `app.js` | `renderCats()` | category id |
| `app.js` | `renderGrid()` | product id |
| `app.js` | `renderUsers()` | user id |
| `admin.js` | `renderProductTable()` | product id |
| `admin.js` | `renderEditBarcodes()` | barcode |
| `admin.js` | `renderMissing()` | product id |

`renderCart()` already had equivalent logic plus an extra fallback for the cart
emptying entirely, and was deliberately left alone. `admin.js` carries a **duplicate
copy** of the helper — the two files share no module — so keep them in step.

**A trap worth remembering:** the helper must move the roving tab stop *before*
calling `.focus()`. `makeRoving()`'s `sync()` parks the single stop on the first
child, so the restore target is sitting at `tabindex="-1"`; focusing it first
silently does nothing. Cost one round of hardware testing to catch.

⏳ **Verified on hardware for the login list only** — selecting a user with Enter now
keeps the arrows live, and selection (green) is visibly independent of focus (blue
ring). The category strip, product grid and the three admin lists are the same code
path but sit behind a PIN, so they still want a human pass.

---

## 1c. ✅ The kiosk could strand the cashier — FIXED 2026-08-24

Found the hard way. A service restart wipes sessions (they live in memory in
`_sessions`), and the consequences were worse than "please log in again":

**a) `/admin` dead-ended the kiosk.** `GET /admin` called `require_admin_session()`,
which raises — and FastAPI renders that as a bare `{"detail": "no session"}` JSON body.
Chromium runs `--kiosk`: no address bar, no back button, no tabs. The cashier was stuck
on a white JSON page with no way out short of SSH. Now returns `303` to `/`, where the
login overlay already lives.

**b) Any page error could do the same.** Added a `StarletteHTTPException` handler:
`/api/*` still returns JSON (the front-end parses it), every other path gets a real
styled page with a **"Volver a la caja"** button. Covers 404s on mistyped paths and
anything added later, not just the `/admin` case.

**c) Session loss looked like broken hardware.** With a stale session the scanner
reported *"Error al leer"* — the caller's generic message — sending someone to hunt a
scanner fault when the real answer was "log in again". `api()` now detects a `401` on
anything except `/api/login` and calls `sessionLost()`: clears state, returns to the
login overlay, and says *"La sesión terminó. Vuelve a entrar."* The scan handler no
longer stacks a second, misleading toast on top.

**Worth knowing:** sessions are in-memory, so *every* deploy that touches Python logs
everyone out. Static-only changes (`app.js`, CSS) can be installed without a restart and
do not. Persisting sessions across restarts is a real improvement, not yet done.

---

## 1d. 🟢 Printer + drawer wired into checkout — 2026-08-24 (untested on paper)

First real Phase 3 work. `devices.py` previously only *detected* hardware; there was no
printing code at all.

**New `app/printer.py`** — ESC/POS for 58 mm thermal, 32 columns. Two rules drive it:
printing must never break a sale (it runs *after* the commit, and every entry point
returns `(ok, detail)` rather than raising — a jammed printer must not undo a paid
sale), and failure must be visible (the outcome is returned to the client and shown).

Codepage is set explicitly to **CP437**, which carries `ñ` and the accented vowels;
verified on the register that `peñafiel 2lt` encodes to `0xA4`. Long product names
**wrap rather than truncate** — truncating made `Coca-Cola Taparosca 500ml` and
`Coca-Cola Taparosca Sin Azucar 500ml` print identically, so a customer checking their
ticket could not tell which they paid for.

**`/api/sale` now**, after committing: prints the receipt, kicks the drawer, and returns
`printed` / `drawer` / `test_mode`. A failed print writes a `receipt_failed` audit row;
**every** drawer opening is audited including the automatic one, so the trail does not
look suspiciously sparse at close of shift. The till toasts specifically — "NO se
imprimió el ticket", "el cajón no abrió", or both — because the cashier's next move
differs (reprint vs reach for the key).

**Test mode** — `meta.test_mode`, admin-only via `GET`/`PUT /api/admin/settings`,
audited as `test_mode_changed`. Sales are still recorded normally; only the two physical
side effects are suppressed. The point is rehearsing the flow without burning a roll of
paper, not faking the books.

- Admin: new **Ajustes** view with a large touch-friendly switch.
- Till: an amber **MODO PRUEBAS · no imprime** pill in the header whenever it is on,
  fed from `/api/bootstrap`. A silent test mode is a trap — the cashier presses COBRAR,
  no ticket appears, and they conclude the printer is broken.

⏳ **Nothing has actually been printed yet.** Rendering is verified, the write path is
verified (`/dev/usb/lp2`, service user in group `lp`), but no paper has moved and the
drawer has not been fired. ⚠️ **`test_mode` currently defaults to OFF**, so the next
`COBRAR` prints and opens the drawer for real.

**Reprint + corte added 2026-08-24.** `POST /api/receipt/reprint` reprints the last
real sale (refunds excluded) stamped `*** COPIA ***`, so a copy can never be passed off
as a second sale; not admin-gated, since a customer asking for a ticket that did not
come out is routine, but audited as `receipt_reprinted` so a run of them is visible.
Bound to **F17**, which is no longer dead. Deliberately not scoped to the open shift —
"el último ticket" means the last thing that came out of the printer.

Shift close now prints the **corte de caja**: opening float, sales count and total, each
retiro *with its envelope number* (a lump sum makes a missing envelope invisible),
expected vs counted, and `SOBRA`/`FALTA`, with a signature line. Printed after the close
is committed — a jammed printer must never block closing the till — and a failure is
audited as `corte_print_failed` and surfaced to the cashier.

**Cerrar turno now confirms first (2026-08-24).** The button sits in the header beside
everyday controls and ends the session irreversibly, so it asks *"¿Desea cerrar su turno
y hacer corte de caja?"* before doing anything. Confirming **opens the drawer**, because
counting the cash requires it open and making the cashier press a second key for that is
busywork. Best-effort: a drawer that will not open does not block the close — the key
still works. Escape cancels; the macropad's O key confirms.

Still missing for Phase 3: nothing on the printing side. Hardware verification of all
of it remains outstanding.

---

## 1e. 🟢 Steren COM-8234 Wi-Fi adapter — WORKS, in-kernel driver, 2.4 GHz only

**Corrected 2026-08-24.** An earlier note in this file called it "the wrong part" and
said it needed an out-of-tree DKMS driver. **That was wrong on both counts.**

What actually happened the first time: `usb-modeswitch` **was not installed**, so the
adapter sat in its driver-CD mode (`0bda:1a2b`, `Product: DISK`), tried to switch itself,
and the port failed to re-enumerate. Installing `usb-modeswitch` + `usb-modeswitch-data`
(Debian ships a udev rule for `0bda:1a2b` specifically) fixed it. A second wrong claim —
that `rtl8xxxu` was absent from this kernel — came from `modinfo` not being on the SSH
`PATH`, not from the driver being missing.

**After mode switch it is fully working:**

| | |
|---|---|
| USB id | `0bda:b711` — "RTL8188GU 802.11n WLAN Adapter (After Modeswitch)" |
| Actual chip | **RTL8710BU** rev A (UMC), 1T1R |
| Driver | **`rtl8xxxu`, in-kernel** — no DKMS, nothing to rebuild on kernel upgrades |
| Firmware | `rtlwifi/rtl8710bufw_UMC.bin`, rev 16.0 (from `firmware-realtek`) |
| Interface | `wlx502b73a03712`, MAC `50:2b:73:a0:37:12` |
| Verified | scans and sees every house SSID at 100% on ch 11 |

So [[PLAN]]'s DKMS objection does **not** apply to this device. Two real caveats remain:

1. **2.4 GHz only** — `iw phy` reports Band 1 and nothing else. [[PLAN]] wanted 5 GHz
   because the Tera 5100 scanner dongle is proprietary 2.4 GHz. Worth watching for
   interference in practice; not fatal.
2. **It lands on VLAN 50, not VLAN 10.** No SSID here maps to MGMT. That is fine for
   internet and fine for SSH *into* the till (the ACL is inbound on Vlan50, so VLAN 10 →
   VLAN 50 is unrestricted), but `ACL-USERS-IN` denies `10.0.50.0/24 → 10.0.0.0/24`
   wholesale — so **the register could not drain its sync outbox to the Phase 5 backend
   over Wi-Fi** without a permit added for its address.

**Connected 2026-08-24 as a standby**, NM profile `CasaVistaHermosa-standby`:
autoconnect on, `ipv4.route-metric 700` against Ethernet's 100, so Ethernet always wins
and Wi-Fi only carries traffic if the cable drops.

```
default via 10.0.0.2   dev enp0s31f6        metric 100   <- primary
default via 10.0.50.1  dev wlx502b73a03712  metric 700   <- standby
```

Verified on the Wi-Fi interface specifically (`ping -I`, `curl --interface`): VLAN 50
gateway reachable, HTTPS 200, signal **-30 dBm**, 72.2 Mbit/s on ch 11. Register now
holds `10.0.50.234` in addition to `10.0.0.22`.

### 🟠 Security consequence, needs a decision

The till is now reachable from **VLAN 50 (Users Wi-Fi)**, and `sshd` listens on
`0.0.0.0:22` with **`passwordauthentication yes`** and no firewall. Before this it was
VLAN 10 only — behind the SSH-ACL trust boundary. Any client on the house Wi-Fi can now
reach the register's SSH and attempt password auth, and the Networking repo already flags
🔴 that one password is reused across the whole fleet.

Options, in order of preference:
1. **`PasswordAuthentication no`** — key auth already works for both `gus` and `tienda`,
   and physical console access at the till remains as recovery. Cheapest real fix.
2. Firewall SSH to `10.0.0.0/24` sources only — keeps a password path but closes the
   Wi-Fi exposure. Note this also blocks SSH when Ethernet is down, which is exactly the
   scenario the adapter exists for.
3. Accept it, on the grounds that VLAN 50 is a trusted user network. Weakest, given the
   shared password.

---

## 2. 🔴 Barcode label PDF prints no bars

**Status: root cause confirmed by opening the saved PDF.**

`~/Documentos/Hojas de Codigos de barras/Hoja de codigos 24-8-2026.pdf` on the
register renders the product name, the price, the 13 digits and the dashed label
border — **but the bars are blank space.** A label sheet that doesn't scan is worse
than no label sheet, because it looks fine until someone tries to use it at the till.

**Cause:** `admin.css:85`

```css
.label .bar { width:2px; background:#101418; }
```

Each bar is a `<div>` whose only visual is a **background colour**. Chromium omits
background graphics when printing unless the user ticks "Background graphics" in the
print dialog, and `print-color-adjust: exact` is **not set anywhere** in either
stylesheet (verified). Text and borders are foreground, which is why everything else
on the label survived and only the bars vanished.

**Fix — two options:**
- *Quick:* add `print-color-adjust: exact; -webkit-print-color-adjust: exact;` to
  `.label .bar` (and probably `.label` itself).
- *Robust, preferred:* render the barcode as an inline **SVG** with `<rect fill>`.
  SVG shapes are page content, not decoration, so they print regardless of the
  background-graphics toggle and regardless of who is standing at the printer. For
  something whose entire job is to be machine-readable, do not leave it depending on
  a checkbox in a print dialog.

**Also verify while in there:** bar width and quiet zone at real print scale — 2px
CSS bars may not survive scaling to a label sheet. Test by scanning an actual
printout with the Tera 5100, not by eye.

---

## 3. 🟠 Live search in the main catalogue

Pin a search field beneath the category strip on the sell screen.

- **Filters as you type** — no submit button, no debounce long enough to feel slow.
- Match on **product name and barcode** (a clerk holding a damaged label can type the
  digits).
- `Enter` adds the top hit to the cart; `Escape` clears and returns to the category
  view.
- Arrow keys walk the results — reuse the existing `makeRoving()` helper in `app.js`
  rather than writing a second focus model.

⚠️ **The hard part is the scanner, not the search.** The Tera 5100 types like a
keyboard, and `app.js` has a global scan buffer watching keystrokes. A focused text
input will swallow those keystrokes. Decide explicitly how the two coexist —
likeliest answer is that a scan is detectable by inter-keystroke timing and should be
routed to the scan handler even when the search box has focus.

This is already anticipated in [[PLAN]] — "Barcode gaps … needs a fast search-and-tap
fallback", and open question 5 notes items like loose cups and ice will never have a
scannable code.

---

## 4. 🟠 Rest of the admin panel

Today `admin.html` has exactly two views: `#viewProducts` and `#viewBarcodes`.
Everything else in the nav is still to build.

- **Users** — create/edit cashiers, reset PINs, activate/deactivate. The two-role
  target model is already specified in [[PLAN]] §"Users and roles"; this is the UI
  for it. Note PIN length is already role-dependent (4 for cashier, 6 for admin).
- **Reports** — sales by day / by shift / by product; cash reconciliation against
  the `corte de caja`; shift history; cash-movement (retiro) audit trail.
- **Sync status** — the header shows a bare count ("28 por sincronizar"). An admin
  view should show what's queued and let someone confirm it drained.

**Scope call to make:** local-only reports can be built now against the register's
own SQLite. Cross-register and historical reporting properly belongs to the Phase 5
backend. Don't build the same report twice — decide which lives where before starting.

---

## 5. 🟢 Macropad hotkeys — WORKING 2026-08-24 (LEDs: not possible on stock firmware)

### What the device actually is

Fully characterised over VIA raw HID. Ignore the keycap legends (`SL`/`PS`/`PB`) —
they describe nothing about what the firmware sends.

| | |
|---|---|
| USB id | `feed:6060`, strings `PGONSTI` / `keyboard` — these are QMK's **default** VID/PID and a placeholder product string, so the device is unsearchable online |
| MCU | **ATmega32U4**, signature `1E 95 87` (family `0x1E`, product `0x95`, rev `0x87`) |
| Bootloader | **Atmel DFU `03eb:2ff4`** — entered via VIA `id_bootloader_jump` (0x0B), left via `dfu-programmer atmega32u4 start` |
| Matrix | **2 rows × 3 cols** — 6 positions, 5 keys, one empty beside the large pair |
| VIA | protocol **9**, 4 layers, 16 macro slots, 938-byte macro buffer |
| HID | 3 interfaces: boot keyboard, raw HID (usage page `0xFF60`), mouse/consumer |
| LEDs | 5 per-key + 1 underside, RGB, running a cycling animation |

### The keymap (remapped 2026-08-24)

All five keys now send **F13–F17**, written to **all four layers**. Writing every layer
matters: the first attempt set layer 0 only and produced *no key events at all*,
because the pad was sitting on another layer (its stock layer 1 was media keys).

| Key | Sends | Physical position |
|---|---|---|
| `r0c0` | F13 | large clear cap, **O** marking |
| `r0c1` | F14 | large clear cap, **X** marking |
| `r1c0` | F15 | small, left of the trio |
| `r1c1` | F16 | small, middle |
| `r1c2` | F17 | small, right |

F13–F17 is a deliberate choice: the Tera 5100 scanner is a keyboard wedge that can only
emit digits and Enter, the browser binds nothing in that range, and a cashier cannot
produce F13 by accident. No modifier needed, no possible collision with a scan.

**The stock keymap is saved at `macropad-backup/stock-keymap.json`** (all 4 layers × 6
positions) and can be restored over VIA at any time.

### Bindings, live in `app.js`

The two large caps are already marked **O** and **X**, so they took confirm/cancel:

- **F13 (O)** — **confirm the purchase**: `COBRAR` on the sell screen, or the primary
  button of whatever overlay is open (pay, shift open, retiro, shift close).
- **F14 (X)** — **cancel**: with an overlay open it means "back" and mirrors Escape;
  on the bare sell screen with a cart it triggers *Cancelar venta*. That action keeps
  its admin override — the hotkey is a shortcut to the button, not a way around it.
- **F15** — Consultar precio.
- **F16** — **Abrir cajón**, no admin required (see below).
- **F17** — deliberately unbound. Nothing sensible left to bind; left dead rather than
  given a surprising meaning.

Handled *before* the `S.activeKeypad` branch in the global keydown listener, because
that branch swallows every non-digit key while an overlay is open — exactly where
confirm and cancel matter most.

### `POST /api/drawer/open` — new, 2026-08-24

Opening the drawer is **not** gated on an admin override, unlike `/api/cash/drop`. A
cashier needs the drawer for change on essentially every sale; a till that demands a
manager for that is a till whose drawer gets propped open all morning, which is worse
than what the gate was protecting against. Accountability comes from the audit row
instead: `drawer_opened`, with the user id and whether the hardware actually fired.

**The audit row is written even when the kick fails** — "the drawer was opened and the
printer was dead" and "nobody tried" are different stories, and shift reconciliation
depends on telling them apart.

Mechanism: the drawer has no bus of its own, it hangs off the printer's RJ11 port, so
opening it is an ESC/POS kick (`ESC p 0 25 250`) written to the printer node.
`devices.open_drawer()` never raises — a dead drawer must not take a sale down with it.

Verified: node present at `/dev/usb/lp2`, service user `tienda` is in group `lp` and
can write to it, endpoint returns 401 without a session.
⏳ **Not yet fired on real hardware** — deliberately left for a human keypress rather
than popping the drawer open remotely. This is the first piece of Phase 3 (printer /
drawer / receipts) to exist at all; `devices.py` previously only *detected* hardware.

### 🔴 LED colour control is NOT possible without replacing the firmware

The goal was "confirm key glows green". It cannot be done as things stand, and this was
established by test, not assumption:

1. **VIA lighting channels are dead.** Probed all four (`backlight`, `rgblight`,
   `rgb_matrix`, `led_matrix`) for brightness/effect/speed/colour. Every **get** returns
   zeros; every **set** is acknowledged and does nothing — verified visually over a
   webcam while sending "solid green". Matches the Megalodon docs: *"RGB cannot be
   adjusted directly in VIA software"* — colour is reachable only through QMK lighting
   **keycodes**, i.e. device-side, the opposite of what the till needs.
2. **The LEDs run a cycling animation anyway** — the pad came back cyan after a power
   cycle having been red. Even a working colour set would be overwritten by the effect.

**Custom QMK firmware is the only route, and it is one-way:**

- 🔴 **The stock firmware cannot be backed up.** The chip's security bit is set:
  `dfu-programmer dump` fails with `Failed to read 28672 bytes`, and the EEPROM read
  fails too. Writing still works, so flashing is possible — but **there is no way back
  to the original firmware.** Not difficult; gone.
- 🔴 **No QMK board definition exists for this pad**, and writing one needs the row/col
  pins and the LED data pin. That is not discoverable in software — it means opening the
  case and tracing, or flashing probe firmwares blind, which is a bad loop on a device
  with no restore path.

### ⚠️ Open question: is the RGB even per-key?

Gus's observation, and it matters more than the firmware question: the pad may have a
**single global colour**, not per-key control. Everything observed is consistent with
that — all five keys plus the underside changed together (all red, then all cyan after
a power cycle), which looks the same whether it is one global channel or addressable
LEDs running a synchronised effect. **Software cannot tell the two apart**; it needs
eyes on the board to see whether the LEDs are a WS2812 chain or a single backlight
channel.

If it is one global colour, "confirm key glows green" is impossible at *any* firmware
level, and the most a custom build could do is change the whole pad's colour to signal
state. That would be a much weaker feature than the one we set out to build.

**Recommendation: don't flash.** The hotkeys — the actual value — already work at zero
risk. Flashing is irreversible, needs the case opened for pin assignments, and may turn
out to buy nothing if the LEDs are globally driven. That is a poor trade for a QoL
device that is not part of the register's core function. Revisit only if someone opens
the case and confirms addressable per-key LEDs.

The AVR toolchain (`dfu-programmer`, `avrdude`, `avr-gcc`, `make`) is already installed
on Gus's PC, and udev rules for `0xFEED` and Atmel DFU are in `/etc/udev/rules.d/`, so
the groundwork is done if it is ever picked up.

---

## Related

[[PLAN]] · Networking repo: [[network-topology]] · [[NETWORK-TODO]]
