---
tags: [cashregister, todo, backlog]
---

# Cash Register — Backlog

Running backlog for the register, opened **2026-08-24** from real use rather than
speculation. See [[PLAN]] for the phase plan and `backend/README.md` for the central
service; this file is what is left, why, and what was already fixed.

Numbered sections below are kept **after** they are fixed, with the root cause intact —
several of these bugs shared a cause, and the record of *why* has been worth more than
a tidy list. Start with **State of play** for the current picture.

---

## State of play — end of 2026-08-29

Everything queued that morning shipped, plus three things found during the day.
The shop ran on the backup register while the till was restarted. Sections below
keep their diagnosis intact, per this file's convention.

### Shipped

| | Item | Where | Verified by |
|---|---|---|---|
| Q1 | Receipt/report local time | `printer.py`, `admin.js` | A sale at `01:30Z` prints **19:30 on 2026-08-29** — the date bug |
| Q2a | Cart survives a restart | `app.js` localStorage | Same-cashier/same-shift guard; suppressed on `sessionLost` |
| Q2b | Admin panel on an admin PIN | `main.py`, `app.js` | **Used in production** — Betsy (`role=cashier`) elevated twice, audit rows 00:40 / 00:42Z |
| Q3 | Admin panes scroll independently | `admin.css` | Confirmed on the live screen |
| Q4 | Pack/single stock pooling | central schema + API | Platos **−12 → 888**, Vasos **29 → 1450** |
| Q5 | Agregar Efectivo | `main.py`, `db.py`, `printer.py`, `app.js` | **Used in production** — float_in $30 at 18:31, $90 at 18:34 |
| Q6 | Apagar la caja from the UI | `main.py`, `app.js`, polkit | Powered the till off cleanly at the end of the day |

Found and fixed the same day, not queued beforehand:

- **`Registrar entradas` overflowed its column.** `.recvActions .primary` never
  overrode `style.css`'s `width:100%/height:68px/font-size:24px` from the till's
  COBRAR button, so it took the whole column and rendered under the recent-entries
  list. Pre-existing; Q3 merely made it visible.
- **Central's "Hoy" reported tomorrow.** `iso()` used `toISOString()` (UTC), so from
  18:00 local the reports asked for the next day. Now pinned to
  `America/Mexico_City` with `Intl`, matching what the server groups by.
- **The printed label sheets.** Four faults on the cut-out sheet (half the page
  blank from a hidden grid track, labels sliced at page breaks, barcodes missing
  after page 1 because a fragmented `<svg>` does not paint, and a `position:fixed`
  toast stamped on every page), then scan spacing on the binder sheet — which in
  turn exposed `PER_PAGE` being coupled to `.bCell`'s padding. See §Q3/§Q4 notes
  and the commits.

### Infrastructure added

- **WayVNC** (`wayvnc.service`, Debian 13 package) — remote view/control of the
  kiosk screen, bound to `127.0.0.1:5900`, reached over an SSH tunnel. `BindsTo`
  the kiosk so it follows its lifecycle. **The till still exposes only port 22.**
  Apple's Screen Sharing cannot talk to it (wayvnc offers only RFB security type
  `None`); RealVNC Viewer works.
- **`/etc/polkit-1/rules.d/50-cashregister-power.rules`** — four login1 action ids
  for user `tienda` only, excluding the `*-ignore-inhibit` variants. The app holds
  no sudo.
- **A MacBook key** (`SHA256:9bgwO+F4…`, `gus@macbook`) added to `gus`'s
  `authorized_keys`.

### Data

- **Seven pack/single pairs pooled**: Corona, Modelo, Vasos, Platos, Tenedores,
  Cucharas, and Vaso Térmico (20/pack, bound later the same evening).
- **PepsiCo delivery recorded** — 12 lines, 58 units, $1001.08, matching the nota.
- **Six catalogue weights corrected** on central and pulled down.

### Still needing a human check

- **The cashier → elevate → `/admin` → back path** is proven up to the panel
  (Betsy did it), but the *return* to the sell screen with a live cart has not been
  exercised.
- **Print one binder sheet** after the `PER_PAGE = 20` change: no page should be
  without a heading, and Botanas should span exactly two sheets.

### Open, small

- **The admin panel labels an elevated cashier "Administrador".** Betsy is
  `role=cashier`; the header is hardcoded for anyone who gets in. Cosmetic, but the
  screen asserts a role the audit log correctly denies.
- **Central has no `no-cache` middleware.** Static files carry an ETag but no
  `Cache-Control`, so every console deploy needs a manual hard refresh. The till has
  had this middleware since a stale cache blanked its screen; central never got one.
- **Central has no deploy script.** Changes go out by `scp` to `/opt/caja/app` plus
  `systemctl restart caja-api`. Check `md5sum` against `git show HEAD:` first —
  there are stale `.bak` files on the box from earlier hand-edits.
- **`REGISTER_HOST` is stale again.** `ssh -G cashregister` resolves to
  `10.0.0.22`, the wired address, which is down while the till is on Wi-Fi. Every
  deploy needs `REGISTER_HOST=gus@10.0.50.101` until the new cable is run.

### ⚠️ Consequence of Q2b, still undecided

`require_admin` tries the typed PIN against every admin, and there is exactly **one** (`Gustavo Aréchiga`, id 3).
`MAX_FAILURES = 5`, `LOCKOUT_SECONDS = 300`. So **five fumbled elevation PINs lock
the only admin out of the till for five minutes** — including out of closing a
shift with a shortfall. This was always true of the shift-close override, but that
is rare; the elevate button makes admin-PIN entry routine, so the lockout will now
actually be reached. Options: leave it (5 min is short), exempt elevation from the
failure counter, or add a second admin.

---

## The queue — opened and cleared 2026-08-29

All six shipped; see State of play above. Kept in full because the diagnosis is
worth more than the tick, and several of these shared a cause.

Originally deferred because the shop was trading; all of it went out the same day
on the backup register. No item needed a reboot — restarting
`cashregister.service` was enough for all of them.

**A backend restart still logs the cashier out** — sessions live in a plain dict in
memory (`main.py:48`), so any restart invalidates them and the next API call 401s
into `sessionLost()`. That much is unchanged and is the correct behaviour on a
machine handling cash.

✅ **But the cart now survives it.** Q2a mirrors it to `localStorage` and restores it
after the same cashier logs back into the same shift, so a deploy, a crash or a
kiosk reload no longer costs a half-scanned sale. This was the standing hazard the
whole queue had to work around, and it is why Q2a was ordered before everything that
needed a restart.

### Q1. 🔴 Receipts print UTC, not local time

`printer.py:99,101` slices the stored timestamp as text instead of converting it:
`sold_at[11:16]` for the time and `sold_at[:10]` for the date. `db.py:21` stores
correct UTC (`datetime.now(timezone.utc)`), so **the data is right and only the paper
is wrong**.

- Verified 2026-08-29: ticket #3 rung at **14:01** local printed **20:01**.
- The clock, timezone (`America/Mexico_City`) and NTP on the till are all **correct** —
  this is not a clock problem, and setting the timezone fixes nothing.
- **The date is the sharper edge:** sales after **18:00** local print *tomorrow's* date,
  because UTC has already rolled over. A customer ticket dated a day ahead is the kind
  of thing that gets questioned at a return.
- **It is not only the printer.** `admin.js:623` renders the Entradas recientes list
  with `(x.received_at||'').slice(5,16).replace('T',' ')` — the same raw-UTC slice. The
  delivery registered at 14:34 local displays as `08-29 20:34`. Five sites in total:
  `printer.py:99,101,153,155` and `admin.js:623`.
- **Do not change how `sold_at` is stored** — Caja Central's UI already converts it
  correctly (`app.js:21`, `new Date(s).toLocaleString('es-MX')`), and re-basing storage
  would break that and every row already synced. `admin.js` can borrow that same
  one-liner; only `printer.py` needs a Python-side helper.

### Q2. 🟠 Admin panel from the cashier account, PIN-gated

**Purpose, in Gus's words (2026-08-29): reach the admin panel WITHOUT closing the
shift.** Today the only way in is to log out of the cashier account and back in as
admin, which ends the shift — so a five-second barcode fix costs a shift close and
reopen. The button removes that. Uses named: **edit barcodes, register stock
(entradas de mercancía), and the rest of the panel.**

Scope is **the full admin panel**, not a barcode-only slice. That is safe as built:
`admin.js:640-668` already polices its own boundaries in central mode — it renders
banners saying catalogue, prices and costs are administered in Caja Central, and
disables `#newProduct` and `#genCode`, while leaving barcode scanning and receiving
fully live. The panel's four views are Productos e inventario, Códigos de barras,
Entradas de mercancía, and Ajustes. Nothing extra needs restricting.

**The real problem is the cart, not authorisation.** `/admin` is a separate page, and
the cart lives only in browser memory (`app.js:62`, `S.cart`) with no `localStorage`
backing. Navigating away from the sell screen silently destroys a half-scanned sale —
the §1c stranded-cashier failure in a new form.

Three ways out, best first:

1. **Persist the cart to `localStorage`** and restore on load. Fixes this *and* the
   existing hazard where any kiosk reload drops a scanned cart. Makes plain navigation
   to `/admin` safe, and is the smallest change with the widest benefit.
2. Render the panel in an overlay/iframe on the sell page, so it never unloads.
3. Navigate, but refuse when the cart is non-empty — worst option: it blocks the fix at
   exactly the moment a bad barcode is discovered, mid-sale.

Notes for whoever picks it up:

- `askOverride(what, onOk)` (`app.js:806`) already does the whole PIN ceremony: amber
  "Autorización requerida" card, 6 dots, auto-submit on the sixth digit, inline
  "PIN incorrecto" with retry. Reuse it; do not write a second PIN prompt.
- **It must verify server-side.** `app.js:799` records that an earlier version trusted
  any six digits, and that was the bug.
- The admin endpoints need `require_admin_session`, and the cashier holds a *cashier*
  session — so the server needs a short-lived elevated session. The existing override
  passes `admin_pin` per action, which suits one-shot acts, not a panel visit.
- Decide the elevation window (suggest a few minutes, dropped when the panel closes or
  a sale completes). A PIN pad on a shop-floor kiosk is shoulder-surfable.
- PIN checking and lockout already exist in `auth.py` (`LOCKOUT_SECONDS`, `db.py:104`),
  and lockout state is till-owned — no new auth concept needed.
- `#adminLink` (`index.html:18`) is the existing header slot, hidden for non-admins at
  `app.js:630`.

### Q3. 🟠 "Entradas de mercancía" scrolls the whole page instead of the list

**Symptom**, confirmed on the live screen 2026-08-29: once "Entradas recientes" has
enough rows, the *page* scrolls rather than the list. The header, the four nav tabs and
the toolbar all scroll off the top, and the left column's **Registrar entradas** button
and Nota field end up stranded mid-page, overlapping the recent list. The scrollbar on
the right is the browser's, not the pane's. Gus wants the list to scroll on its own —
hover it, scroll it, the rest of the tab stays put.

**The markup is already right — do not change it.** `#recvRecent` (and `#recvList`)
already carry `class="scroll"`, and `.scroll { flex:1; overflow:auto }` is correct.

**Root cause is the root height, in `admin.css:5-6`:**

```css
html, body { overflow:auto; height:auto; min-height:100%; }
#adm       { display:flex; flex-direction:column; min-height:100vh; }
```

Neither has a *definite* height — only `min-height`. The whole chain below it
(`#body{flex:1;min-height:0}` → `.view{flex:1;overflow:auto}` → `.split{flex:1;
min-height:0}` → `.col` → `.scroll{flex:1;overflow:auto}`) is correctly built, but
`flex:1` has nothing bounded to resolve against, so every container grows to fit its
content and `overflow:auto` never has a constrained box to overflow inside. The
overflow lands on the document instead.

**Fix direction:** give the root a definite height for screen — `html,body{height:100%;
overflow:hidden}` and `#adm{height:100vh}` in place of `min-height` — which lets the
existing flex chain cap itself and makes every `.scroll` pane scroll internally. This
fixes the products table (`.tableWrap`) and the barcode columns at the same time, since
they share the pattern.

⚠️ **Check the label sheet before shipping it.** `admin.css:137` has an `@media print`
block for the barcode sheet, which needs the document to grow across pages. Scope the
height clamp to screen so printing is untouched, and print-test one sheet — §2 records
that the sheet has been silently broken before.

### Q4. 🟠 Pack-and-single products — beer and disposables — stock silently drifts

Six product families are each **two unrelated products** with no shared stock, so the
two halves cannot stay accurate. Live figures on central, 2026-08-29:

| id | Product | Price | Received | Sold | on_hand |
|---|---|---|---|---|---|
| 22 | Corona Light 355ml | $22.00 | 216 | 0 | **216** |
| 23 | Corona Light Six Pack | $130.00 | 0 | 0 | **0** |
| 131 | Modelo Especial 355ml | $25.00 | 227 | 2 | **225** |
| 132 | Modelo Especial Six Pack | $150.00 | 0 | 0 | **0** |
| 135 | Vasos Paquete (50) | $50.00 | 29 | 0 | **29** |
| 136 | Vasos Individual | $2.00 | 0 | 0 | **0** |
| 133 | Platos Paquete (50) | $50.00 | 18 | 0 | **18** |
| 134 | Plato Individual | $2.00 | 0 | 12 | **−12** ⚠️ |
| 139 | Tenedores Paquete (25) | $25.00 | 43 | 0 | **43** |
| 140 | Tenedor Individual | $2.00 | 0 | 0 | **0** |
| 137 | Cuchara Paquete (25) | $25.00 | 13 | 0 | **13** |
| 138 | Cuchara Individual | $2.00 | 0 | 2 | **−2** ⚠️ |

**The two families are mirror images, and that matters for the design:**

* **Beer** is received against the **single** (216 bottles, 227 bottles) and the pack
  products have received nothing. Nothing has drifted yet only because no pack has sold.
* **Disposables** are the opposite — received against the **pack** (29, 18, 43, 13) and
  sold as individuals. **This one is already broken:** Plato Individual is at
  **−12** and Cuchara Individual at **−2**, while 18 packs of plates sit unaccounted.

So the drift is not hypothetical, and it runs in both directions depending on which half
of the pair gets received.

**The model: one stock pool per family, counted in the smallest unit** (a bottle, a cup,
a fork). Give every sellable product two fields — `stock_product_id` (the pool it draws
from) and `units_per_sale` (how many base units one of it is):

| Product | `stock_product_id` | `units_per_sale` |
|---|---|---|
| Corona Light 355ml (22) | 22 (itself) | 1 |
| Corona Light Six Pack (23) | **22** | **6** |
| Modelo Especial 355ml (131) | 131 (itself) | 1 |
| Modelo Especial Six Pack (132) | **131** | **6** |
| Vasos Individual (136) | 136 (itself) | 1 |
| Vasos Paquete (135) | **136** | **50** |
| Plato Individual (134) | 134 (itself) | 1 |
| Platos Paquete (133) | **134** | **50** |
| Tenedor Individual (140) | 140 (itself) | 1 |
| Tenedores Paquete (139) | **140** | **25** |
| Cuchara Individual (138) | 138 (itself) | 1 |
| Cuchara Paquete (137) | **138** | **25** |

`Servilletas Paquete` (141) has no individual counterpart, so it stays its own pool at
`units_per_sale = 1` — the case that shows the default is the correct no-op.

**The math.** For a pool `b`, summing over every product `p` that maps to it:

```
on_hand(b) = Σ [ received(p) − sold(p) ] × units_per_sale(p)
```

Applied to today's data that gives: Corona **216** bottles, Modelo **225** bottles,
Vasos **1450** (29×50), Platos **888** (18×50 − 12), Tenedores **1075** (43×25),
Cucharas **323** (13×25 − 2).

**The multiplier MUST apply to `receiving`, not just to sales.** For beer it looked
optional, because stock arrives already counted in bottles. For disposables it is the
whole point: without it Vasos reads 29 instead of 1450. This is the single most
important line in the design — a sales-only multiplier silently under-counts every
pack-received product by a factor of 25 or 50.

Today's query is the special case where every product is its own pool with a multiplier
of 1, so the shape of `backend/app/main.py:512` barely changes — it gains a join to the
mapping and two `* units_per_sale` factors. Display can derive the human form with
`divmod(on_hand, n)`: 216 bottles reads as "36 six-packs, 0 loose"; 888 plates as
"17 paquetes, 38 sueltos".

**Why this is cheap here, specifically:**

- **The till needs no schema change.** It has no stock column and never computes
  on_hand (`db.py:390`, `schema.sql:265`) — it only emits `receiving` events per
  product_id. The whole change is central's derivation plus two catalogue fields that
  ride down on the existing pull.
- **History corrects itself.** on_hand is *derived*, never stored, so the multiplier
  applies retroactively the moment it ships. No backfill, no migration of past rows, and
  no cleanup of the 12 platos and 2 cucharas already sold — the two negative balances
  above turn correct on their own.
- **Breaking open a pack becomes a non-event.** Both products draw from one pool, so
  opening a paquete to sell singles needs no adjustment at all. That is precisely what
  is happening with plates and spoons today, and it is why those two are negative.

**Decisions to make before building:**

1. **Prices stay independent — do not derive them.** A six-pack is $130 against
   6 × $22 = $132, while a 50-cup paquete is $50 against 50 × $2.00 = $100. Singles
   carry a deliberate 2× markup on disposables. Only *stock* unifies; pricing must stay
   free.
2. **Cost per base unit.** When a pack is received, divide the landed cost by
   `units_per_sale` so the pool holds one consistent unit cost.
3. **Reorder levels must be in base units**, or the low-stock panel compares cups to
   paquetes and is wrong by 50×. Related: only 92 of 154 products have one at all.
4. **Never block a sale on stock.** A negative pool is a reporting signal, not a
   checkout error — same principle as printing never breaking a sale. Plato Individual
   is at −12 right now and nothing should have stopped those sales.
5. **Which half is the pool?** Always the individual, even when nothing is ever received
   against it (Vasos Individual has received 0). The pool is a unit of measure, not a
   product that must see traffic.

⚠️ **Data bugs spotted while pulling these numbers**, all central-owned and unrelated to
the logic above:

- `cost_cents == price_cents` on ids 22, 23, 132, and on all four disposable individuals
  (134, 136, 138, 140 — $2.00 cost against $2.00 price). 131 and every paquete have no
  cost at all. The margin column is meaningless for both families until fixed.
- Inconsistent singular/plural naming: `Platos Paquete` / `Plato Individual`,
  `Tenedores Paquete` / `Tenedor Individual`, `Cuchara Paquete` / `Cuchara Individual`,
  `Vasos Paquete` / `Vasos Individual`. Cosmetic, but it makes the pairs harder to spot.
- The pack sizes are **not** recorded anywhere in the data today — they live only in the
  product name (`Vasos Paquete`) or in nobody's head at all. `units_per_sale` becomes the
  first place the shop actually states that a paquete is 50.

### Q5. 🟢 "Agregar Efectivo" — cash in, the mirror of Retiro

A button that records cash **added** to the drawer and opens it, exactly like Retiro
parcial but with the sign reversed. Bound to the macropad key that currently reprints
the last ticket.

**Most of this already exists.** The data model was built for it and never wired up:

| Piece | State |
|---|---|
| `cash_movement.kind` accepts `'float_in'` | ✅ `schema.sql:147` |
| Expected cash already **adds** it | ✅ `schema.sql:247` — `CASE WHEN kind='float_in' THEN amount_cents ELSE -amount_cents END` |
| `db.cash_movement()` is generic on `kind` | ✅ `db.py:288` |
| Outbox already carries `cash_movement` | ✅ `schema.sql:181` |
| Caja Central already labels it | ✅ `backend/app/static/app.js:1082` — `float_in: 'Fondo'` |

So the shift arithmetic and the reporting are **done**. Nothing about the close-of-shift
maths needs touching, which is the part that would have been risky.

**What is actually missing:**

1. `POST /api/cash/float_in` — a mirror of `cash_drop` (`main.py:405`). Ungated for the
   same reason retiros are: an unrecorded deposit is worse than an unwitnessed one.
   Audit it as its own action. **No envelope number** — envelopes tie paper bags in the
   safe to money leaving; money arriving has no bag.
2. UI overlay mirroring `#dropOverlay` / `openDrop()` / `renderDrop()`.
3. Open the drawer as part of the flow — `/api/drawer/open` takes a free-form `reason`
   string (`main.py:399`), so `"efectivo"` needs no schema change.
4. Rebind the macropad key.

**The two real gaps, both in the shift report:**

- `db.shift_summary()` (`db.py:346`) selects `WHERE kind = 'drop'` only. Cash added
  would move `expected_cents` while appearing nowhere on the report — a manager
  reconciling at close would see the expected figure jump with no line explaining it.
  That is exactly the kind of unexplained gap that reads as theft.
- `printer.py:175-177` hardcodes a `"-"` sign and a `"Retiro sobre %s"` label. Float-ins
  must print as their own section, not be folded into `drops`, or they will print as
  negatives and double the error.

⚠️ **Rebinding costs the reprint feature entirely.** `reprintLast()` is reachable
**only** from F17 — there is no on-screen button anywhere in `index.html`. Replacing it
leaves no way to print a duplicate ticket for a customer who asks. Decide one of:

- put reprint on-screen (in the guarded row, where Cancelar lives), then take the key; or
- accept losing it, on the grounds that it has rarely been used.

Taking the key without deciding is the one outcome to avoid.

📎 **Key naming:** "PB" is a **keycap legend**, not what the firmware sends. §5 records
that the legends (`SL`/`PS`/`PB`) describe nothing about the output. The key that
reprints today is **F17** (`app.js:214`), so PB = F17 is the binding to change.

### Q6. ✅ Shut the till down from the UI — SHIPPED 2026-08-29

Deployed. After **Cerrar turno** succeeds the till now offers *"Apagar la caja"*,
with *"Solo cerrar sesión"* alongside it. `POST /api/power` takes
`{"mode":"poweroff"|"reboot"}`, refuses while a shift is open, requires a session,
audits as `power_poweroff` / `power_reboot`, and delays the actual call by 1.5 s so
the response reaches the browser before the socket dies.

Authorisation is `/etc/polkit-1/rules.d/50-cashregister-power.rules` — four action
ids, one user, and deliberately NOT the `*-ignore-inhibit` variants, so a shutdown
inhibitor (a deploy mid-flight) still wins. The app holds no sudo. Verified:
`CanPowerOff` returns `yes` for `tienda` and still `challenge` for `gus`.

Guards verified against a database copy, with an open shift fabricated to trigger
the refusal: invalid mode → 422, open shift → 409 `shift_open`, no session → 401,
and zero audit rows written because nothing was authorised.

⚠️ **The happy path is deliberately untested** — running it powers the till off. It
needs one supervised run at the till.

The original diagnosis follows.

### Q6 (original) — No way to shut the till down from the UI

Raised 2026-08-29 after the till vanished from the network mid-session: the
cashier had almost certainly powered it off, because **the physical button is the
only way to do it**. There is no Apagar anywhere in the sell screen or the admin
panel.

That is not a convenience gap, it is a data-integrity one. A cashier who wants to
close up has exactly two options — a short press (which *may* trigger a clean
`systemctl poweroff` via logind, unverified) or holding the button, which cuts
power. The NVMe already carries **154 unsafe shutdowns**, and § "Both disks are
healthy — the problem is power" traces the earlier filesystem corruption to
exactly this class of event. Every closing shift is another chance to add one.

**Shape of the fix:**

- Put it where closing up already happens: after **Cerrar turno** succeeds, offer
  *"Apagar la caja"*. That ties shutdown to the end-of-day flow, which is the only
  routine reason to do it, and means the shift is provably closed first.
- A second entry point in the admin panel (Ajustes) for the non-routine case.
- **Guards, in order:** refuse with an open shift; refuse with a non-empty cart;
  then confirm. The confirm pattern already exists (`#cancelConfirmOverlay`, added
  when clearing the cart started asking). A mis-tap must never power off a till
  mid-service.
- Audit it like any other guarded action — `db.audit(c, "shutdown", ...)` — so a
  power-off has a name against it and is distinguishable in the log from a crash.
- Offer **Reiniciar** alongside it. Half the reasons to reach for the power button
  are "it is behaving oddly", and a reboot is the safer answer.

**Privilege — ANSWERED 2026-08-29, and the cheap option is out.** Queried on the
live till:

```
busctl ... org.freedesktop.login1.Manager CanPowerOff   ->  s "challenge"
busctl ... org.freedesktop.login1.Manager CanReboot     ->  s "challenge"
```

`"challenge"` means polkit demands interactive authentication, which a kiosk app
cannot answer — so the hoped-for "the session already permits it" route does NOT
work, despite `tienda` owning the active session on seat0. This needs an explicit
grant:

1. A polkit rule allowing only `org.freedesktop.login1.power-off` (and `.reboot`)
   for `tienda`. Preferred — narrowest, and it keeps logind doing the shutdown.
2. A single sudoers line: `tienda ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff`.

Do NOT give the app broad sudo for this. It is one action and should stay one
action.

### ✅ The physical button is already safe on a SHORT press

`systemd-analyze cat-config` shows both settings commented out, i.e. at systemd's
defaults:

```
#HandlePowerKey=poweroff          -> a short press ALREADY powers off cleanly
#HandlePowerKeyLongPress=ignore   -> systemd ignores a long press
```

Corroborated by the 2026-08-29 incident itself: the cashier pressed the button and
`last` recorded a proper `shutdown` at 18:03, with no ext4 errors on the next boot,
`PRAGMA quick_check` = ok and `/mnt/backup` remounted unaided.

**So the immediate fix is telling the staff: press it briefly, never hold it.** That
costs nothing and removes most of the risk today, with or without the UI button.

⚠️ **Holding it cannot be fixed in software.** After ~4 seconds the firmware cuts
power below the OS; `HandlePowerKeyLongPress=ignore` only stops *systemd* acting, it
cannot stop the hardware. A UI button and staff instruction are the only defences.

📊 **NVMe Unsafe Shutdowns now reads 155**, against 154 recorded on 2026-08-28. One
more somewhere in between — there have been three boots since, so it cannot be
pinned on the 18:03 event specifically. Not alarming on its own (155 against 901
power cycles, `Media and Data Integrity Errors: 0`, `Percentage Used: 1%`) but it is
the counter to watch.

---

## Implementation plan for the queue — drafted 2026-08-29

Verified against the code, not assumed. Two facts govern the whole plan:

**1. `tools/deploy.sh` restarts BOTH services** — `cashregister.service` *and*
`cashregister-kiosk.service`. So a normal deploy logs the cashier out (in-memory
sessions), clears the cart via `sessionLost()`, *and* reloads Chromium. Every till-side
deploy must happen with an empty cart, between customers or at close.

**2. `REGISTER_HOST` is stale again.** `ssh -G cashregister` resolves to **10.0.0.22**,
the wired address, which is down while the till is on Wi-Fi. Until the new cable is run,
every deploy needs `REGISTER_HOST=gus@10.0.50.101`. The §"Still open" note claiming
plain `tools/deploy.sh` works again was true only while wired.

### Order, chosen to minimise disruption

| # | Item | Touches | Till restart? | Can run while trading? |
|---|---|---|---|---|
| 1 | **Q4** pack/single stock | Central only | **No** | ✅ Yes — till untouched |
| 2 | **Q2a** cart persistence | `app.js` | Yes, once | ❌ Empty cart only |
| 3 | **Q3** scroll CSS | `admin.css` | No (static file) | ⚠️ Next page load |
| 4 | **Q1** receipt time | `printer.py` + `admin.js` | Yes | ❌ Empty cart only |
| 5 | **Q5** Agregar Efectivo | `main.py` + `app.js` + `printer.py` | Yes | ❌ Empty cart only |
| 6 | **Q2b** admin button | `app.js` + `main.py` | Yes | ❌ Empty cart only |

**Q5 pairs naturally with Q1** — both touch `printer.py`, both need the same empty-cart
restart, and both want a printed ticket to verify. Doing them in one session halves the
disruption and the paper.

**Q4 goes first** because it is the only item that touches nothing on the till — pure
central schema and derivation. Zero risk to trading, and it can be done at any hour.

**Q2 splits.** Q2a (persist the cart to `localStorage`, restore after re-login) is small,
self-contained, and **makes every later deploy safe** — it removes the empty-cart
constraint that otherwise governs items 4 and 5. Paying its one-time restart early buys
that back. Q2b (the button and elevated session) is the large piece and goes last.

### Per-item notes on collateral damage

**Q1 — receipts.** Five sites, not two: `printer.py:99,101` (ticket time and date),
`printer.py:153,155` (shift report `opened_at` / `closed_at`), and `admin.js:623` (the
Entradas recientes list). The four Python ones all read `now_iso()` UTC strings, so
**one** `_local()` helper fixes them; `admin.js` reuses central's existing
`toLocaleString('es-MX')` one-liner instead. It must never raise — the
module's first rule is that nothing here throws into the checkout path — so wrap the
conversion and fall back to the current raw slice. `astimezone()` with no argument uses
the system zone, which is verified correct on the till. Reprints go through the same
`build_receipt`, so they are fixed for free. **Test after 18:00 local**, since that is
when the date bug appears.

**Q3 — scroll.** The two-line root-height clamp is the whole fix, but the `@media print`
block (`admin.css:137`) must then explicitly restore `html, body { height:auto;
overflow:visible }`. Without it, `#sheetGrid` — which is `position:absolute` inside the
clamped body — risks being **clipped to a single page**, silently breaking the label
sheet again. Print one sheet before calling it done.

**Q4 — pack/single stock.** The derivation lives in **two** places: the
`v_stock_on_hand` view (`backend/schema.sql:229`) and an inline copy in the catalogue
query (`backend/app/main.py:512`). Change both or, better, make the endpoint use the
view — two hand-maintained copies of the same arithmetic will drift. Default the new
columns to `(stock_product_id = id, units_per_sale = 1)` so every existing product
behaves exactly as today and the change is a no-op until a mapping is set. **The till
needs no change at all:** it records "one paquete arrived" as an ordinary `receiving`
row, and central multiplies at derivation time — which also keeps the retroactive
property. **Apply the multiplier on both sides of the subtraction**, receiving as well
as sales; a sales-only version under-counts Vasos by 50×. Check any report that groups
by `product_id` before shipping.

**Q5 — Agregar Efectivo.** The risky half — the expected-cash arithmetic — is already
correct in `v_shift_expected`, so do **not** touch it. The work is an endpoint, an
overlay, a keybinding, and the two shift-report gaps (`db.py:346` filters to `'drop'`;
`printer.py:175` hardcodes the minus sign and the "Retiro" label). Settle the reprint
question before rebinding F17, since that key is the feature's only entry point.

**Q2 — admin button.** Keep `askOverride()`; do not write a second PIN prompt. The
elevated session must expire on its own and must be verified server-side. For Q2a,
decide what the persisted cart is keyed to — it must **not** survive a genuine logout or
reappear for a different cashier, only across a restart or reload for the same session.

---

## State of play — 2026-08-28 (the till is wired)

Three things were fixed today: the wired port, sync, and backups. All verified on the
live hardware.

### The wired port was never dead — it was on the wrong VLAN

The 2026-08-25 note below says `carrier=0`, "no link at all... not a VLAN
misconfiguration". **That was wrong once the cable was actually run.** On 2026-08-27
16:21 the cable went into `STR-Store-SW-6 Gi1/0/2`, and the till side came up perfectly:
carrier up, 1000 Full, static `10.0.0.22/24`, default via `10.0.0.2`.

The port had **no configuration at all** — a bare `interface GigabitEthernet1/0/2`,
38 bytes — so it sat in VLAN 1. The tell was `ip -s link`: **RX 0 bytes / 0 packets**
against ~20 k TX, with every ARP on `10.0.0.0/24` FAILED. Carrier proves the cable;
only RX proves the VLAN.

Fixed by applying the stanza that was already written down in [[PLAN]] and saving it
with `write memory` — the switch reboots itself, so an unsaved change would have
evaporated. The `Te1/1/3` uplink was verified to carry VLAN 10; PLAN's old warning that
it might not has been corrected there.

### Plugging in a dead port was worse than staying on Wi-Fi

This is the part worth remembering. The wired default route has **metric 100** and Wi-Fi
has **700**, so the moment the cable went into a dead port the till started preferring it
and blackholed everything toward VLAN 10:

* Sync to `10.0.0.16:8090` last succeeded **2026-08-27T22:02Z**, then stopped.
* Three `sync_outbox` rows stranded (all shift open/close events, no sales).
* Catalogue and price pulls from Caja Central stopped arriving.
* **Nothing surfaced this.** Sales still rang up and stored locally; the till was
  isolated, not down.

A wired link that is up but wrong outranks working Wi-Fi. Failover only protects against
loss of carrier, not against a port that is silently in the wrong VLAN.

After the fix: ping `10.0.0.22` 0.44 ms / 0 % loss (Wi-Fi was ~8 ms), outbox drained to
**0**, sync endpoint returns 200 in 8 ms. **`tools/deploy.sh` works plainly again** — the
`REGISTER_HOST=gus@10.0.50.101` workaround is no longer needed.

### Backups had been failing since 2026-08-26

`backup-status.json` read `{"ok":false,"detail":"/mnt/backup is not mounted"}` and two
nights were missed. The USB disk was NTFS and had gone **dirty**; the kernel refused it
with `volume is dirty and "force" flag is not set!`. `ntfsfix` found real damage —
`$MFTMirr does not match $MFT (record 3)` — and repaired it.

⚠️ **`"remote":null` in that status file did not mean the off-machine copy failed.** The
script `fail()`s at the mount check and exits long before the scp step, so `remote` was
never evaluated. Do not chase a second bug that is not there.

### The backup disk is now ext4, not NTFS

NTFS was the wrong filesystem for a machine that loses power: it goes dirty on every
unclean shutdown and then fails **silently**, because the fstab entry is `nofail`.

```
UUID=0f3119de-4750-4537-a4a5-081b97de2f3b /mnt/backup ext4 defaults,nofail 0 2
```

* **The UUID changed.** The old NTFS `1E1612F31612CC21` is gone; any reference to it is stale.
* `mkfs.ext4 -F -L regbackup -m 1` — `-m 1` because the default 5 % root reserve wastes
  ~23 GB on a pure backup disk. The `ntfs3` options (`uid`/`gid`/`umask`) are meaningless
  on ext4 and were dropped.
* **`nofail` was kept deliberately** — a missing backup disk must never stop the till
  booting. It is still why failures are silent, so the fix for *visibility* is alerting,
  not removing `nofail`. But ext4 journals and self-recovers, so the dirty-volume failure
  mode is gone.

Safe because the disk held only the 7 snapshots (168 KB) plus Windows leftovers; the
107 MB `df` showed was NTFS metadata. All 7 were staged off, restored, and **md5-verified
identical**. Verified after: `umount` + `mount -a` remounts cleanly (so fstab is right
without a reboot), a backup run gives `{"ok":true,"remote":true}`, and a snapshot was
**restored end-to-end** — gunzip, `integrity_check` ok, 14 tables, 2 shifts, 182 products.

### Both disks are healthy — the problem is power

`smartmontools` is now installed and `smartd` monitors both disks.

| | Backup HDD `ST500LM030` | System NVMe `CT500P2SSD8` |
|---|---|---|
| Health | PASSED, short self-test **completed without error** | PASSED |
| Life | 9 225 h | 7 719 h · 1 % used · 100 % spare |
| Bad sectors | 0 reallocated / pending / offline-uncorrectable / reported-uncorrect | 0 media errors |

⚠️ **Seagate `Raw_Read_Error_Rate` ~49.6 M and `Seek_Error_Rate` ~212 M are not faults** —
those raws are composite counters. Normalized 077 and 083 against thresholds 006 and 045
is healthy. This looks alarming every single time; it is not.

The real signal is elsewhere: `Power-Off_Retract_Count` **80** and `G-Sense_Error_Rate`
**21** on the HDD, and **154 unsafe shutdowns** on the NVMe, with `UDMA_CRC_Error_Count`
at 0 clearing the USB cable. **The `$MFT` corruption came from unclean power loss, not
failing hardware** — i.e. the `STR-Store-SW-6` power fault, still unresolved. `ping
10.0.0.6` shows ~50 % loss, and the till logged 253 link up/down events in the 30 h before
the fix. ext4 contains the damage; it does not fix the cause. The UPS still matters.

### Zero sales is correct, not data loss

The backup logs `verified: 0 sales`, which makes the script's live-vs-copy guard pass
trivially (0 == 0). Confirmed genuine: `sale`, `sale_line` and `cash_movement` are all
empty, only 2 test shifts closed at `counted_cents 0`, and the **immutable**
`audit_event` table (UPDATE/DELETE raise) holds 12 events with no sale or void actions.
This matches the deliberate reset recorded under 2026-08-25 — everything to date was
testing. Nothing was lost.

### The keyring dialog on every boot — fixed

The till booted into a GNOME **"Unlock Login Keyring — Authentication required"** modal
sitting on top of the kiosk, with a password box no cashier can answer.

Cause: the kiosk session autologins (`PAMName=login`, no password ever typed), so PAM
cannot unlock `~tienda/.local/share/keyrings/login.keyring`. Chromium was started with no
`--password-store`, so it auto-detected gnome-libsecret and asked for the keyring on
startup, which raised the prompt.

Fix: `--password-store=basic` in `cashregister-kiosk.service`. The till keeps no passwords
in the browser, so a plaintext store costs nothing. **The unit is tracked at
`register/provision/cashregister-kiosk.service`** — it was changed there as well as live,
or the next provision run would have silently reintroduced the dialog.

Ruled out, so nobody re-checks them: NetworkManager stores the Wi-Fi PSK in a **system**
connection (no `permissions=`), so it never needs the keyring; and the prompt is not
`gcr-prompter` left running from something else.

**Confirmed by a real reboot**, not just a service restart: the till came back with no
keyring dialog and `tools/shot.sh` showing the cashier login screen with *Escáner: Listo*
and *Impresora: Lista*.

### Reboot test — everything comes back unaided

First reboot since the VLAN, ext4 and keyring changes. **Back in ~85 s**, answering on the
**wired** `10.0.0.22` — which by itself proves the switch config survived (it was
`write memory`'d) and the static wired config comes up with no help. `/mnt/backup`
auto-mounted as ext4 from the new fstab UUID, all three services active, sync 200 in 7 ms,
outbox empty.

⚠️ **`configure-printer@usb-*.service` fails on every boot and always has.** It is CUPS's
`udev-configure-printer` trying to auto-add the POS-58; the till prints to **raw usblp**,
not CUPS. Expected noise in `systemctl --failed` — do not "fix" it.

⚠️ **The printer node moves between boots** — it returned as `/dev/usb/lp4`, not `lp0`.
Harmless only because `register/app/devices.py` globs `/dev/usb/lp*`. Never hardcode it.

### Still open

- **`STR-Store-SW-6`'s power fault** — now the documented root cause of the disk
  corruption, not just a reliability worry. Tracked in the Networking repo.
- **Nothing delivers alerts.** `backup-status.json` sat `ok:false` for two days unseen,
  and `smartd` now mails `root`, which goes nowhere on this box. Real monitoring, dead
  endpoint — this is the notifications item, and it has teeth now.
- Everything under **Still open** for 2026-08-25 below except the wired port and the
  deploy workaround, both of which are resolved above.

---

## State of play — end of 2026-08-25 (move day)

The register moved to the store. Everything below is deployed to both sides and
verified against the live pair. Read [`docs/INVARIANTS.md`](docs/INVARIANTS.md) before
extending any of it — most of today's bugs were one rule being broken in a new place.

### Barcodes now belong to the till

Ownership was split and the two halves contradicted each other: the pull repointed
codes and resurrected deleted ones, so **only adding a code survived a round trip** —
deletions and reassignments silently reverted seconds later. That cost an afternoon of
re-scanning.

| | |
|---|---|
| Owner | **The till.** The scanner is there; every code is assigned by scanning it |
| Pull | Insert-only. Never repoints a code, never resurrects a deleted one |
| Deletions | Stated via `barcode_tombstone`, not inferred from absence |
| Push | Sends codes central lacks **or has pointed at the wrong product**, plus tombstones |
| Latency | A barcode edit sets a dirty flag → reaches central in ~30 s, not 10 min |
| Verify | `tools/check-barcode-sync.sh` — read-only, exits non-zero when out of sync |

Central still *generates* the internal `2303311` series, because the label sheet prints
there. Those codes arrive by the pull and come back unchanged.

### Deleting products — real, with tombstones

`DELETE /api/catalogue/products/{id}` refuses (409) anything with sales or receiving
history and offers deactivation instead. A `deleted_product` tombstone tells the till to
remove it too; **ids are never reused**, or a tombstone would later delete a new product
that inherited the id.

### Scan-to-receive on the till

Deliveries are scanned in rather than typed. Scan → type the quantity → Enter; rescanning
the same item increments it. Rows carry a till-minted UUID and drain through
`sync_outbox`, and central applies them keyed on `receiving.source_id UNIQUE`, so a
re-sent batch cannot double a delivery.

### Sell screen

Categories are alphabetical with **Todos los productos** and **Frecuentes** pinned on
top; product tiles are alphabetical too. Search sits at the bottom of the category
sidebar and spans the whole catalogue.

### Caja Central

| | |
|---|---|
| Catálogo | Search and category filter **survive an edit** — they used to reset on every save |
| Inventario | Untracked products show their real count; blanking it caused a double entry |
| Etiquetas | Three modes — **Completa** (whole catalogue), **Carpeta** (selection), **Recortar** (stickers) |
| Carpeta | For the counter binder: one category per page, true-size barcodes, 22 mm binding margin |
| Barcodes | **EAN-8 now encodes correctly** — it produced unscannable symbols for weeks |

### Reset

Sales, shifts, cash movements, audit and outbox were wiped on both sides — everything to
date was testing. Ticket numbering restarted at 0. The catalogue was untouched. The
immutability triggers were dropped and **recreated in the same transaction**; all seven
verified back in place.

### Backups were silently broken — fixed

The move put the till on Wi-Fi (`10.0.50.101`), and `ACL-USERS-IN` permitted only its
sync to `10.0.0.16:8090`. The nightly backup copies over **SSH**, which was never
permitted — it had never needed to be, because backups were designed for the wired
VLAN 10 path. `backup-status.json` had been reporting `"ok":true,"remote":false` since
the move: local copy fine, off-machine copy silently absent. **The only surviving copy
of the till's data was on the till itself**, at the store, behind a switch with a known
power fault.

Two fixes, and the first alone was not enough:

* `ACL-USERS-IN` **seq 118** on `TRZ-Core-SW-2` — `permit tcp host 10.0.50.101 host
  10.0.0.16 eq 22`, placed before the `120 deny`, saved with `write memory`.
* `regbak`'s key was pinned `restrict,from="10.0.0.22"` — the *wired* address — so SSH
  connected and was then rejected on the key. Now `from="10.0.0.22,10.0.50.101"`, which
  keeps working when the wired port is repaired.

Verified `"remote":true` with a fresh dump on central. The previous off-machine copy was
over 24 hours old and predated the move, the reset and every schema change.

### Cancelling a sale now confirms

It takes no admin PIN (nothing is recorded before COBRAR and no money moves), but it
used to wipe the basket on the first press — and X on the macropad sits one key from O.
The dialog names what is about to be lost (`3 artículos · $90.00`) rather than asking
about nothing. Enter or **O** confirms, Escape or **X** backs out. All four paths
verified on the hardware.

### Reported bugs — verified status

| # | State |
|---|---|
| 1 | **Open, and it moved.** The till no longer creates products; Central's product sheet has no barcode field, and Central has no scanner |
| 2 | Fixed — the Códigos de barras screen lists generated internal codes |
| 3 | Fixed — Cerrar turno opens the drawer |
| 4 | Fixed — cancelling takes no PIN (verified on the register) |
| 5 | **Mostly done** — retiro takes no PIN and the drawer opens before the amount is asked; still needs the X to dismiss. Untested, nobody on site to close the drawer |

### Still open

- ~~**The wired port is dead.** `enp0s31f6` shows `carrier=0`...~~ ✅ **RESOLVED
  2026-08-28, and the diagnosis above was wrong.** `carrier=0` only meant no cable was
  plugged in yet. Once one was, the fault *was* a VLAN misconfiguration — `Gi1/0/2` had no
  config and sat in VLAN 1. See § State of play — 2026-08-28.
- ~~**Deploy over Wi-Fi** needs `REGISTER_HOST=gus@10.0.50.101`~~ ✅ **No longer needed** —
  plain `tools/deploy.sh` works again.
- **Bug #5** — the dismiss X on the retiro dialog, and the macropad X wired to it.
- **Five active products have no barcode**, so they cannot be sold or received by
  scanning: Buscapina, Canelitas, Fuzetea - Durazno 600ml, Maruchan - Habanero and
  **Maruchan - Pollo** (which lost both its codes during the 2026-08-25 reassignment).
  Gus is waiting on stock before scanning them.
- **Only 92 of 154 active products have a reorder level.** The other 62 report as
  `untracked` and can never appear in low-stock alerts, so that panel silently covers
  60% of the catalogue. Worth a bulk pass before Semana Santa.
- **`apply_central.py`** (5 price changes plus category and spelling fixes from the
  Agosto price sheet) was never run and is stale — rebuild it from live data first.
- **Digital receipts / CFDI import** for bulk stock, once suppliers provide them.
- **Duplicate-ish products** flagged by the hunt: `167 jugo del valle 413ml` needs a
  human call, and `190 acuatica` carries seven EANs on one product.

---

## State of play — end of 2026-08-24

One long session. Everything below is deployed and pushed to GitHub.

### The till — working and verified on real hardware

| | |
|---|---|
| Keyboard | `latam` in the kiosk; `ñ` and accents confirmed |
| Navigation | Arrows step one row; focus survives every re-render; focus visible on links and table rows; Escape exits the admin panel |
| Receipts | Print on every sale (CP437). Real sales, **0** `receipt_failed` rows |
| Drawer | Fires and is audited *with a reason* — `sale`, `shift_close`, `retiro`, `manual` |
| Corte de caja | Prints at close, each retiro with its envelope number |
| Reprint | Last ticket, stamped `*** COPIA ***` (F17) |
| Labels | Barcode sheets print real bars — **scan-tested against the Tera 5100** |
| Macropad | 5 keys → F13–F17 over VIA. O confirms, X cancels, price check, drawer+retiro, reprint |
| Test mode | Admin toggle; suppresses printing and drawer, amber banner on the till |
| Backups | Nightly, **verified by restoring**, local + off-machine |
| Network | Static `10.0.0.22`; Wi-Fi standby `10.0.50.101` at metric 700, failover tested |

### Caja Central — `http://tienda.mgnt` (or `10.0.0.16:8090`)

| | |
|---|---|
| Access | Password login, 12 h session, **changeable in the UI**; restricted to VLAN 10 at the proxy |
| Resumen | KPIs, sales-by-day chart, low-stock panel, register heartbeats |
| Ventas | Tickets with expandable lines |
| Inventario | Stock states, receiving (negative = merma), reorder levels |
| Catálogo | 207 products — search, create, edit; **pushes down to the till** |
| Etiquetas | Generate internal EAN-13s and print the label sheet |
| Usuarios | Cashiers and admins, roles, PINs — pushed down; last-admin guard |
| Turnos / Reportes | Cortes; date-range reports by day/category/product, printable |
| Sync | Till drains every 30 s and pulls the catalogue; heartbeat when idle |

**Ownership is settled:** central owns products, prices, categories, barcodes and
users. The till owns sales, shifts, cash movements and lockout state. The till's own
admin screens go read-only for anything central owns, so the two cannot diverge.

### Refunds — closed by decision, not by code

Gus, 2026-08-24: refunds are handled by opening the drawer and handing the cash back.
No refund flow will be built. The drawer opening is audited, which is the accountability
that matters here. **This is not an open item.**

Worth knowing, not worth fixing unless it bites:
- The corte shows a **faltante** equal to the refund — `v_shift_expected` is
  float + sales − movements, and nothing records the cash leaving. Over **$50** that
  blocks the close pending an admin PIN; under it, it lands on the cashier as a shortage.
- The item stays counted as sold, so stock will read low by one once stock exists.

Cheap remedy if wanted later: `cash_movement.kind = 'payout'` already exists **and is
already subtracted by `v_shift_expected`** — recording a refund as a payout is the retiro
dialogue with a different kind, and makes the corte balance.

### Next, in order

Backups, catalogue push, receiving, auth, users and barcodes are all **done** — see the
sections below. What is genuinely left:

1. **Record opening stock.** Inventory is built, but every product reads *sin
   seguimiento*: on-hand is `received − sold`, so until someone counts a shelf and sets
   a reorder level, low-stock alerting has nothing to work with. Data entry, not code.
2. **Live product search on the sell screen** — for things that will never have a
   barcode (ice, loose cups). The hard part is the scanner, not the search: the Tera
   5100 types like a keyboard into a global buffer, and a focused input would swallow
   it.
3. **Reissue the internal cert** with `tienda.mgnt` in the SANs so the console can be
   HTTPS. Its SAN list is explicit, not a wildcard.
4. **Notifications that reach a person** who is not looking at the dashboard — the
   low-stock badge only helps someone already in the console.
5. **`10.0.0.31` DHCP exclusion** on the gateway (Networking repo) — still open.

### Backups — done 2026-08-24

**Register** (`cashregister-backup.timer`, 03:20 nightly, `Persistent=true` so a till
switched off overnight still backs up on next boot):

- `sqlite3 .backup` — a consistent snapshot of a *live* WAL database. `cp` would produce
  a file that looks fine and restores short.
- **Verified before it counts as good:** `pragma integrity_check` must return `ok`, and
  the snapshot's sale count is compared against the live database. It may be one behind
  if a sale landed mid-copy, never ahead.
- Written to the **spare 500 GB SATA disk** at `/mnt/backup` (`nofail`, so a missing
  disk cannot block boot), gzipped, 60 days retained.
- **Copied off the machine** to `regbak@10.0.0.16:/var/backups/register/` over a key
  restricted with `restrict,from="10.0.0.22"`. An unreachable backend is a warning, not
  a failure — a local-only backup still beats none.
- Result written to `/var/lib/cashregister/backup-status.json` so a failure is visible
  somewhere other than a journal nobody reads.

**Central** (`caja-backup.timer`, 03:40 — after the till's, so a night's sales have
synced first): `pg_dump -Fc`, TOC verified readable and checked for the tables that
matter, 60 days retained.

**Proxmox**: weekly `vzdump` of container 116, Sundays 04:15, `keep-last=3`.

**Restore was actually tested**, not assumed: the dump restored into a scratch database
with **0 errors** and every row count matching (`sale` 10, `sale_line` 15, `shift` 20,
`cash_movement` 2, `register` 1), views included. Scratch database dropped afterwards.

🟠 **Found while doing this:** the Proxmox host had **no backup jobs at all** — including
for `trz-vault-15`, which is Vaultwarden. I added one for container 116 only, since that
is this project's. The other two are someone's call, but a password vault with no
backups is worth raising.

### Wi-Fi sync — enabled 2026-08-24

The till can now reach the backend over its Wi-Fi standby, so syncing survives the
Ethernet cable being out — the case that matters at the store.

- **DHCP reservation** `10.0.50.101` (pool `CAJA-WIFI`, client-id `0150.2b73.a037.12`).
  Its Wi-Fi address was a dynamic lease, and an ACL permit written against a lease breaks
  on renewal. `.101` is inside the gateway's existing `10.0.50.100-109` exclusion.
- **`ACL-USERS-IN` seq 117:** `permit tcp host 10.0.50.101 host 10.0.0.16 eq 8090` —
  one host, one port. **Not** the whole subnet: the backend UI has no auth, so a
  subnet-wide permit would expose the sales history to every phone on the house Wi-Fi.

Verified by forcing traffic out the wireless interface: `:8090` returns 200, Proxmox on
`.13:8006` stays blocked. Then Ethernet was taken down entirely — the till stayed
reachable at `10.0.50.101`, routed solely over Wi-Fi, and kept draining. Restored by an
armed rollback timer.

⚠️ **The spare disk is still NTFS.** It works (mounted `ntfs3`, verified writable) and I
did not reformat it without asking. ext4 would be the better home for this; it is a
one-line change whenever you want it.

### Known smaller issues

- A service restart clears the cart mid-sale (sessions are in-memory). Any Python
  deploy logs the cashier out; **static-only changes do not** — prefer those when
  someone is using the till.
- **A static deploy needs a hard reload of the kiosk browser.** Restarting the service
  does not reload the page. This cost a round of confusion when "Cerrar turno doesn't
  open the drawer" turned out to be correct code the browser had never loaded.
- ~34 product names lack accents (inherited from Aronium, not a bug). Gus's call.
- Macropad LEDs are not host-controllable and may be a single global colour; flashing
  is one-way. Parked.

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

## 1f. ✅ Five workflow fixes from real use — 2026-08-24

1. **New products accept barcodes before saving.** The barcode controls refused to work
   until the product existed, so adding one meant save → find it again in the table →
   reopen → add. Codes typed (or "generar") are now queued, shown tagged *al guardar*,
   and applied the moment the product is created. A code that turns out to be taken is
   reported with the panel left open rather than closing over a half-applied change.
2. **The barcode panel now lists every internal code, permanently.** It only ever showed
   products *missing* a code, so generating one made the product vanish from the screen —
   with no way to reprint its label afterwards, which is exactly when you need it (label
   peeled off, damaged sheet, new shelf tag). Each row has *Agregar a hoja*.
3. **Cerrar turno really does open the drawer now.** The code was already correct and
   deployed; the kiosk browser was still running the JS it had loaded hours earlier.
   Restarting the service does not reload the page — **a hard reload is required after
   any static change**. Also tagged: the automatic open is audited with
   `reason: "shift_close"`, so it no longer looks like an unexplained manual open.
4. **Cancelling a sale no longer needs an admin PIN.** Nothing is recorded server-side
   before COBRAR and no money moves, so it only empties a basket on screen. Requiring a
   manager trained people to keep a supervisor's PIN to hand, which is worse for the
   things that genuinely need one.
5. **Retiro opens the drawer first and asks afterwards.** The cashier is mid-service with
   a customer waiting; making them answer "how much?" before the drawer opens adds a
   pause to every retiro. The macropad's Open Box key (F16) now does the same thing, so
   the two are one flow. The dialogue has an **✕** and a *Solo abrir el cajón* button —
   dismissing is a legitimate outcome, since the opening itself is audited either way.
   The macropad's X key closes it too.

---

## 1g. ✅ Deleting a product — added 2026-08-24

There was no delete at all, only the Activo/Inactivo toggle. That toggle **is** the right
tool most of the time (the till's catalogue query filters `is_active`, so an inactive
product vanishes from the sell screen while its history survives) — but it left no way to
remove a product typed in by mistake, which is common and was accumulating clutter.

`DELETE /api/admin/products/{id}`, admin-only and audited as `product_deleted`:

- **Refuses with 409 `has_sales`** if the product appears on any `sale_line`. Those rows
  point at the product id, and stock is derived as `received − sold`, so deleting the row
  they reference corrupts both history and reporting. The receipt snapshot survives, but
  the joins do not.
- Otherwise deletes the product and its barcodes and bumps `catalogue_revision`.

The admin panel shows **Eliminar** only for products that already exist, separated from
Guardar by a spacer so it is not under a thumb aiming at save. On a 409 it offers to
deactivate instead, which is what the user actually wanted in that case.

Verified against live data: a product with sales returned `has_sales` and stayed; a fresh
product deleted cleanly; an unknown id returned `unknown`.

---

## 2. ✅ Barcode label sheet prints real bars — FIXED 2026-08-24

The saved PDF rendered the name, price, digits and dashed border but left blank space
where the barcode belongs. Cause: `admin.css` drew every module as a `<div>` whose only
visual was `background:#101418`, and Chromium omits background graphics when printing
unless the operator ticks "Background graphics" in the dialog.

**Fixed by rendering the barcode as inline SVG `<rect>`s.** SVG is page content, so it
prints regardless of that checkbox — no `print-color-adjust` and no dependence on who is
standing at the printer. Runs of adjacent modules are merged into single rects (30 rects
instead of 95 divs) because at print scale separate 1-module rects can leave hairline
gaps where the renderer rounds edges, and a hairline through a bar is exactly what makes
a scan fail. Quiet zones (11 modules left, 7 right) are now explicit.

Verified offline: 95 modules, guards present at both ends, and the SVG round-trips to a
byte-identical module array — the merging is lossless.

✅ **Scan-tested by Gus and confirmed working.**

**Moved to Caja Central 2026-08-24.** The label sheet prints on an ordinary printer and
there is not one in the store, so generation and printing both belong in the console.
The till's barcode screen is now read-only, and codes push down with the catalogue.

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

## 4. ✅ Admin panel — moved to Caja Central, 2026-08-24

The till's `/admin` had only two views and no users or reports. Rather than grow it,
the whole surface moved to the console, which is the right home: an admin works from a
desk with a real printer, not from the till.

- **Users** — cashiers and admins, roles, PIN resets, activate/deactivate. PIN length
  is enforced by role (4/6), and the console refuses to remove the **last active
  admin**: a register with no admin cannot authorise an override or close a shift with
  a shortfall, and nobody could fix that from the shop floor.
- **Reports** — by day, category and product over any date range, printable to PDF.
- **Sync status** — every register's last heartbeat, on the summary screen.
- **Catalogue, barcodes and labels** — see sections 2 and 5.

The till's own admin turns read-only for anything central owns. Two masters was the
failure mode to avoid: the next pull would silently overwrite whatever was typed there.

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
