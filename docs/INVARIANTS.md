# Invariants

Rules that constrain every new feature. Not a spec — a spec says what the system
does, this says what it is never allowed to do.

Each rule is here because breaking it cost real time. The date is when it bit.
Read this before adding anything that touches sync, barcodes, stock or history.

---

## 1. Two audiences, two design goals

The cashier and the admin are not the same user, and a screen built for one is
wrong for the other.

**Cashier surfaces** — the sell screen, scanning, taking payment, closing a
shift. Optimise for **simplicity, speed and being hard to get wrong**. Peak day
on record is 149 sales in one day of Semana Santa, with a queue. Every decision
put in front of a cashier is a decision made badly under pressure.

- Fewest possible steps; the common path needs no choices at all.
- Big targets, one obvious action, no dense tables.
- The physical controls are part of the UI: the scanner, the macropad's O and X.
- Destructive or money-moving actions are **guarded by an admin PIN**, not by a
  confirmation dialog the cashier learns to dismiss.
- **A PIN and a confirmation answer different questions.** *May you do this?* is
  authorization; *did you mean to?* is confirmation. Cancelling a sale needs no
  PIN — nothing is recorded and no money moves — but it still confirms, because
  X sits one key from O on the macropad and clearing a part-scanned basket means
  re-scanning it in front of the customer. Conversely, a retiro needs a PIN but
  opens the drawer *first*, so a manager hunt never delays the queue.
- **A confirmation must name what it is about to destroy.** "¿Está seguro?" gets
  dismissed on reflex within a week; "3 artículos · $90.00" is a number the
  cashier can check against the basket in front of them.
- It must work with the network down (§9).

**Admin surfaces** — the console, the till's admin panel. Optimise for
**completeness, detail, manageability, and keeping the two sides in agreement**.

- Show everything, including what is inactive, unmatched or untracked.
- Filters, search and counts are features here, not clutter.
- Every screen should make disagreement between till and central *visible* —
  a code only one side has, stock that has not synced, a pending outbox.
- Slower and denser is fine. Being wrong quietly is not.

The failure mode is mixing them: putting an admin's density in front of a
cashier, or hiding detail from an admin because it looked untidy. Blanking an
untracked stock count (§10) was the second mistake — an admin screen protecting
someone from a number they needed.

## 2. One owner per kind of data

| Data | Owner | Direction |
|---|---|---|
| Products, prices, costs, categories | **Central** | central → till |
| Users, PINs, roles | **Central** | central → till |
| Barcodes | **The till** | till → central |
| Sales, shifts, cash movements, audit | **The till** | till → central |
| Receiving (stock in) | **Either**, keyed by origin | till → central |

Ownership follows the hardware. Barcodes belong to the till because **the scanner
is physically there** — every code is assigned by scanning it onto a product.
Prices belong to central because that is where someone sits with a spreadsheet.

**Never let two places write the same field.** Barcodes were briefly owned by
both: central's pull repointed codes and resurrected deleted ones, so the only
barcode edit that survived a round trip was *adding* one. Deletions and moves
silently reverted seconds after they were made (2026-08-25).

When adding a feature, name its owner first. If the answer is "both", the design
is wrong.

## 3. Absence never means deletion

A row missing from a sync payload means *nothing*. It does not mean "delete this".

Removals must be **stated**: `deleted_product` and `barcode_tombstone` exist for
exactly this. Tombstones are kept forever — they are two columns, and a till that
was offline for a season still has to learn what went away.

Why: a snapshot that arrives truncated, or a product central has not heard of
yet, would otherwise wipe live data. A till that quietly drops products
mid-season is far worse than one carrying a stale row.

**Corollary — ids are never reused.** `create_product` takes `MAX(id)+1` across
`product` *and* `deleted_product`. Without that, deleting the highest-numbered
product frees its id, and the tombstone still standing against it would tell
every till to delete the *new* product on its next pull (caught 2026-08-25,
before it shipped).

## 4. Every till → central write is idempotent on a till-minted id

The register generates a UUID; central inserts `ON CONFLICT (id) DO NOTHING`.

A link that drops mid-post is the normal case, not the exception. The till cannot
know whether a batch it never got a reply for was applied, so it re-sends — and
that must be free.

`receiving` needed an extra `source_id uuid UNIQUE` column because its primary
key is a `bigserial` the till cannot predict.

This is not theoretical: a hand-entered 6-unit delivery became 12 because the UI
gave no feedback and the entry was made twice (2026-08-25).

## 5. History is immutable

Sales, sale lines and audit events have `BEFORE UPDATE`/`BEFORE DELETE` triggers
that abort. Enforced in the database, not in application code, so a future bug
cannot rewrite the past.

Money data is never *corrected* — it is offset. A refund reverses a sale; a
negative `receiving` row reverses a miscount. Both leave the original visible.

Deliberate admin resets (wiping test data) drop the triggers, wipe, and
**recreate them in the same transaction**. If you do this, verify all seven are
back afterwards.

## 6. Deactivate, don't delete, anything with history

`is_active = false` propagates and preserves the record. Hard delete is reserved
for rows with no history at all, and must be guarded:

- **Products**: refuse (409) if any `sale_line` or `receiving` row references it.
- **Users**: no delete exists. A sale stores only `user_id` — there is no
  `name_at_sale` equivalent — so deleting a cashier makes every past sale of
  theirs attributable to nobody, and `audit_event.by_user` is exactly the record
  that must survive.

## 7. The scanner is a keyboard wedge

The Tera 5100 types digits into **whatever has focus** and presses Enter. It has
no idea what screen you are on.

Consequences, all of which have bitten:

- Any focused input can receive a barcode. A quantity field will happily accept
  `7501055302086` as a quantity unless it checks.
- Distinguish a scan from typing by **burst speed and length**:
  `SCAN_MIN_LEN = 6` digits arriving within 120 ms of each other, ended by Enter.
- A screen that expects scanning must **keep a field focused at all times**. A
  button that disables itself after use will silently swallow focus, and the next
  scan goes nowhere while the screen looks ready.

## 8. A workflow must survive its own success

After any action completes, the screen must be ready for the next one
immediately. This is the same bug three times over:

- Editing a product rebuilt the catalogue view and **cleared the search**, so a
  run of edits meant retyping the query every time.
- Registering a delivery left focus on the now-disabled button, so the next scan
  was lost.
- Restarting the till service to force a sync **logs the cashier out**.

Test the *second* iteration, not the first.

## 9. The till must sell with the network down

Non-negotiable, and the reason for most of the above. Anything a cashier does
during a sale writes locally first and drains later. Nothing in the sell path may
block on central.

Admin screens may require the network. Selling may not.

## 10. Say what is unknown, don't blank it

A stock count with no reorder level is *untracked*, not *unknown quantity* —
`received − sold` is perfectly well known. Blanking it as `—` made a registered
delivery look like it had not saved, so it was entered twice (2026-08-25).

Show the number; qualify the state.

## 11. Two symbologies, always

The catalogue holds EAN-13, UPC-A **and EAN-8**. Any code that renders, encodes
or validates must handle all three:

- UPC-A is EAN-13 with an implied leading zero — 12 digits must match the same
  product stored as 13.
- EAN-8 is 67 modules, not 95, and has no parity word.

`eanBars()` assumed 13 digits and appended the literal string `"undefined"` for
an 8-digit code, producing a plausible-looking symbol that could never scan. Two
products printed broken labels for weeks (found 2026-08-25).

## 12. Print layout is not screen layout

CSS Grid **fragments badly across page breaks** — Chrome ignores
`break-inside: avoid` on grid items. The label sheet split three labels across a
page boundary, leaving their names on one page and their barcodes on the next.

Use `inline-block` for anything that must paginate. Barcodes that will be scanned
off paper need a **locked aspect ratio and real millimetre sizing**, never
`preserveAspectRatio="none"`.

---

## Applying this to a new feature

0. **Who is this screen for?** (§1) Cashier → fewest steps. Admin → most detail.
1. **Who owns this data?** (§2) If both sides, redesign.
2. **How is removal expressed?** (§3) If by absence, redesign.
3. **What happens if the write is sent twice?** (§4)
4. **Does it touch money or history?** (§5, §6) Then it is append-only.
5. **Will someone scan while this screen is open?** (§7)
6. **What does the screen look like immediately after it succeeds?** (§8)
7. **Does it block selling when the network is down?** (§9) Then move it.
