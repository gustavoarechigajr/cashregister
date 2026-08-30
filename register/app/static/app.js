'use strict';

// A kiosk that fails silently is far worse than one with a visible error: on
// a blank screen nobody on-site knows whether to wait, restart, or call for
// help. Any uncaught error or rejected promise -- caught here specifically
// once, found by a genuine stale-cache incident during development -- prints
// on screen instead of leaving a dead login page with no clue why.
function showFatalError(label, err) {
  const d = document.createElement('pre');
  d.style.cssText = 'position:fixed;left:16px;bottom:16px;z-index:999;color:#ff5f5f;'
    + 'background:#000;padding:8px;font-size:12px;max-width:80vw;white-space:pre-wrap';
  d.textContent = label + ': ' + (err && (err.stack || err.message) || String(err));
  document.body.appendChild(d);
}
window.addEventListener('error', e => showFatalError('Error', e.error || e.message));
window.addEventListener('unhandledrejection', e => showFatalError('Error async', e.reason));

const S = { users: [], cats: [], products: [], byId: {}, cat: 'frecuentes', search: '',
            cart: [], session: null, shift: null, checking: false, pcProduct: null,
            pin: '', pinUser: null, ovr: null, ovrPin: '', tendered: '', float: '',
            activeKeypad: null,
            // Pending quantity edit on a focused cart row: typed digits build
            // `buf` as an ABSOLUTE replacement value (never appended to the
            // committed qty). A fresh digit arriving after the buffer has
            // already been committed (pid reset to null) starts a new buffer
            // instead of continuing the old one — typing "2","3" then, later,
            // "1","3" yields 13, not 2313.
            qtyEdit: { pid: null, buf: '', timer: null },
            dropAmount: '', addCash: '', closeCash: '', shiftSummary: null };

// Mirrors the server's own threshold (main.py SHORTFALL_REQUIRES_ADMIN_CENTS).
// Purely a UI prediction so the override PIN can be asked for up front
// instead of after a wasted round trip — the server enforces this for real
// regardless of what the client predicts.
const SHORTFALL_REQUIRES_ADMIN_CENTS = -5000;

const $  = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
const mxn = c => (c < 0 ? '−$' : '$') + Math.abs(c / 100).toLocaleString('es-MX',
                 { minimumFractionDigits: 2, maximumFractionDigits: 2 });

async function api(path, opts) {
  const r = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts));
  if (!r.ok) {
    const e = new Error((await r.json().catch(() => ({}))).detail || r.statusText); e.status = r.status;
    // A 401 on anything except a login attempt means the server no longer
    // knows this session. Sessions live in memory, so ANY restart of the
    // service -- an update, a crash, a power blip -- silently invalidates a
    // cashier who is still looking at a normal-looking till. Before this, the
    // only symptom was whatever generic message the caller happened to show
    // ("Error al leer" on a scan), which reads as broken hardware and sends
    // someone hunting the scanner instead of just logging back in.
    if (r.status === 401 && !path.startsWith('/api/login')) sessionLost();
    throw e;
  }
  return r.json();
}

/* ------------------------------------------------------------ cart recovery
   The cart lives in memory, and sessions live in a dict on the server. So ANY
   restart of the service -- a deploy, a crash, a power blip -- used to take a
   half-scanned sale with it: the next call 401s, sessionLost() empties the
   cart, and the cashier re-scans everything while the customer waits. Same on
   any kiosk reload.

   So the cart is mirrored to localStorage and restored after logging back in.

   Two rules make this safe rather than surprising:
     * Restore only for the SAME cashier in the SAME shift. A cart must never
       reappear under someone else's name, and never survive a shift boundary.
     * A genuine logout, a cancelled sale and a completed sale all clear it.
       Only an involuntary loss is recoverable.

   Every access is wrapped: localStorage throws in a private window and when
   site data is blocked, and losing the sell screen to a storage error would be
   far worse than losing the cart. */
const CART_KEY = 'cashregister.cart.v1';
const CART_MAX_AGE_MS = 12 * 3600 * 1000;   // a shift, generously
let cartSaveSuppressed = false;

function saveCart() {
  if (cartSaveSuppressed) return;
  try {
    if (!S.session || !S.shift || !S.cart.length) { localStorage.removeItem(CART_KEY); return; }
    localStorage.setItem(CART_KEY, JSON.stringify({
      user_id: S.session.id, shift_id: S.shift.id, at: Date.now(), cart: S.cart }));
  } catch (e) { /* storage unavailable — the till still works, just not recovery */ }
}

function clearSavedCart() {
  try { localStorage.removeItem(CART_KEY); } catch (e) { /* see above */ }
}

function restoreCart() {
  try {
    const raw = localStorage.getItem(CART_KEY);
    if (!raw || !S.session || !S.shift) return;
    const d = JSON.parse(raw);
    if (!d || !Array.isArray(d.cart) || !d.cart.length) return;
    if (d.user_id !== S.session.id || d.shift_id !== S.shift.id
        || (Date.now() - (d.at || 0)) > CART_MAX_AGE_MS) {
      clearSavedCart(); return;
    }
    S.cart = d.cart;
    renderCart();
    toast('Se recuperó la venta en curso');
  } catch (e) { clearSavedCart(); }
}

function sessionLost() {
  if (!S.session) return;                 // already sitting on the login screen
  // Deliberately does NOT clear the saved cart: this is the involuntary path
  // the recovery exists for. Suppress the save so the renders below, which run
  // with an empty cart, cannot overwrite what we are about to restore.
  cartSaveSuppressed = true;
  S.session = null; S.shift = null; S.pinUser = null; S.pin = '';
  S.cart = []; S.checking = false;
  renderWho(); renderCart(); renderUsers(); renderPin();
  cartSaveSuppressed = false;
  openOverlay('#loginOverlay');
  toast('La sesión terminó. Vuelve a entrar.', true);
}

let toastTimer;
function toast(msg, bad) {
  const t = $('#toast'); t.textContent = msg; t.className = bad ? 'bad' : '';
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add('hidden'), 2600);
}

/* -------------------------------------------------------------- key routing
   One document-level keydown listener drives everything: scanner input,
   physical-keyboard PIN/amount entry, and Escape-to-cancel. Only one of
   these is "listening" at a time, chosen by what's on screen:

     - a numeric overlay is open (login PIN, admin override, opening float,
       cash tendered)  -> digits/backspace go to S.activeKeypad, Enter
       triggers that overlay's primary action if one is enabled
     - nothing is open (the sell screen) -> digits + Enter are read as a
       barcode scan, exactly as before

   This was previously two disconnected things: an on-screen numpad with
   onclick handlers, and a scan-only listener that ignored everything else.
   Typing a PIN on a physical keyboard did nothing because nothing was
   listening for it. */
let scanBuf = '', scanTimer = null;
// A genuine scan is a burst of digits arriving faster than any human types,
// terminated by the scanner's own Enter. Below this length, Enter came from
// someone actually pressing keys, not the wedge.
const SCAN_MIN_LEN = 6;

function primaryActionFor(overlayEl) {
  if (overlayEl === $('#cancelConfirmOverlay')) return $('#cancelConfirmYes');
  if (overlayEl === $('#closeConfirmOverlay')) return $('#closeConfirmYes');
  if (overlayEl === $('#payOverlay'))        return $('#confirmPay');
  if (overlayEl === $('#shiftOverlay'))      return $('#openShift');
  if (overlayEl === $('#dropOverlay'))       return $('#dropConfirm');
  if (overlayEl === $('#addCashOverlay'))    return $('#addCashConfirm');
  if (overlayEl === $('#shiftCloseOverlay')) return $('#closeConfirm');
  return null;
}

function updateInert() {
  // An overlay is a modal: while one is visible, the sell screen behind it
  // must not be reachable by Tab, even though it is still in the DOM. Without
  // this, Tab order follows markup order, not visual stacking, and a keyboard
  // user tabbing from the login screen lands in the product grid underneath it.
  const anyOpen = $$('.overlay').some(o => !o.classList.contains('hidden'));
  $('#app').toggleAttribute('inert', anyOpen);
}
function openOverlay(id, onKey) {
  $(id).classList.remove('hidden');
  S.activeKeypad = onKey || null;
  updateInert();
  // Skip disabled buttons too (confirmPay starts disabled until an amount is
  // entered) — .focus() silently no-ops on a disabled button, which otherwise
  // drops focus back to <body> with no visible indicator of where keyboard
  // input goes next.
  const first = $(id).querySelector('button:not([tabindex="-1"]):not(:disabled)');
  if (first) first.focus();
}
function sellScreenAnchor() {
  return $('#cats button[tabindex="0"]') || $('#cats button');
}
function closeOverlay(id) {
  $(id).classList.add('hidden');
  S.activeKeypad = null;
  updateInert();
  // Whatever was focused inside this overlay is now display:none, so the
  // browser drops focus to <body> with no visible indicator. If we're back
  // to the plain sell screen (no other overlay took over), give keyboard
  // users a concrete anchor to Tab/arrow from instead of losing their place.
  const anyOpen = $$('.overlay').some(o => !o.classList.contains('hidden'));
  if (!anyOpen && S.session) {
    const anchor = sellScreenAnchor();
    if (anchor) anchor.focus();
  }
}

function currentOverlay() {
  return $$('.overlay').find(o => !o.classList.contains('hidden')) || null;
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    const ov = currentOverlay();
    if (ov === $('#priceOverlay'))      { closePriceCheck(); e.preventDefault(); return; }
    if (ov === $('#payOverlay'))        { closePay(); e.preventDefault(); return; }
    if (ov === $('#dropOverlay'))       { closeDrop(); e.preventDefault(); return; }
    if (ov === $('#cancelConfirmOverlay')) { closeOverlay('#cancelConfirmOverlay'); e.preventDefault(); return; }
    if (ov === $('#closeConfirmOverlay')) { cancelCloseShift(); e.preventDefault(); return; }
    if (ov === $('#shiftCloseOverlay')) { closeShiftClose(); e.preventDefault(); return; }
    if (ov === $('#ovrOverlay'))        { closeOverride(); e.preventDefault(); return; }
    if (ov === $('#loginOverlay') && S.pinUser) {
      S.pinUser = null; S.pin = ''; renderUsers(); renderPin();
      // renderUsers() rebuilds the row buttons, destroying whichever one was
      // focused -- same render-vs-focus trap as everywhere else. Restore it
      // to the roving default, or Up/Down (which only fire via a listener on
      // #userList itself, reached only through a focused child) go dead.
      const first = $('#userList button[tabindex="0"]') || $('#userList button');
      if (first) first.focus();
      e.preventDefault(); return;
    }
    if (S.checking) { setChecking(false); e.preventDefault(); return; }
    return; // shiftOverlay has no cancel — a shift must be opened to proceed
  }

  /* --------------------------------------------------- macropad hotkeys
     The DOIO/Megalodon pad is remapped (over VIA, no firmware flashing) so
     its five keys send F13-F17. That range is deliberate: the Tera 5100
     scanner is a keyboard wedge and can only ever emit digits and Enter, the
     browser binds nothing up here, and a cashier cannot produce F13 by
     accident on the main keyboard. So these need no modifier and can never
     collide with a scan.

     Physical layout, and why: the two big caps are already marked O and X.
       F13  big  O   -> confirm the purchase (COBRAR, or an overlay's primary button)
       F14  big  X   -> cancel   (back out of an overlay, else cancel the sale)
       F15  small    -> Consultar precio
       F16  small    -> Abrir cajon -- no admin, but always audited
       F17  small    -> Agregar efectivo (cash IN -- the mirror of F16)

     F16/F17 are deliberately the pair: retiro and deposito sit next to each
     other physically, and both open the drawer then ask for an amount. F17
     used to reprint the last ticket; that was dropped on request when this
     was added. The server endpoint (/api/receipt/reprint) is untouched, so
     reprint can be put back on a button whenever it is wanted.
     This runs BEFORE the keypad branch below, which swallows every non-digit
     key while an overlay is open -- otherwise confirm/cancel would be eaten
     exactly where they are most useful. */
  if (e.key === 'F13' || e.key === 'F14' || e.key === 'F15' ||
      e.key === 'F16' || e.key === 'F17') {
    e.preventDefault();
    if (!S.session) return;              // nothing to drive on the login screen
    const ov = currentOverlay();
    if (e.key === 'F13') {
      const btn = ov ? primaryActionFor(ov) : $('#cobrar');
      if (btn && !btn.disabled) btn.click();
      return;
    }
    if (e.key === 'F14') {
      // With something open, X means "back" -- mirror Escape rather than
      // duplicating each overlay's close logic. On the bare sell screen it
      // means cancel the purchase. That button no longer asks for an admin
      // PIN (nothing is recorded before COBRAR and no money has moved), so
      // this is simply a shortcut to it.
      if (ov || S.checking) {
        document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      } else if (S.cart.length) {
        const btn = $$('#guarded button').find(b => b.dataset.act === 'cancel');
        if (btn) btn.click();
      }
      return;
    }
    if (ov) return;                      // the two below are sell-screen only
    if (e.key === 'F15') setChecking(!S.checking);
    if (e.key === 'F16') openDrop();
    if (e.key === 'F17') openAddCash();
    return;
  }

  if (S.activeKeypad) {
    if (/^[0-9]$/.test(e.key)) { e.preventDefault(); S.activeKeypad(e.key); return; }
    if (e.key === '.') { e.preventDefault(); S.activeKeypad('.'); return; }
    if (e.key === 'Backspace' || e.key === 'Delete') { e.preventDefault(); S.activeKeypad('←'); return; }
    if (e.key === 'Enter') {
      const btn = primaryActionFor(currentOverlay());
      if (btn && !btn.disabled) { e.preventDefault(); btn.click(); }
      return;
    }
    return; // an overlay is open: never fall through to the scanner buffer
  }

  // ---------------------------------------------------- cart row spinner
  // A focused cart row behaves like a native number input's spinner:
  // Left/Right nudge the quantity by one, digits type an absolute value,
  // and Up/Down move focus to the previous/next line (Tab/Shift+Tab still
  // work too — this is an additional fast path, not a replacement).
  //
  // The scanner is a keyboard wedge — it types into whatever has focus. If
  // the cashier scans a second product while a cart row happens to be
  // focused (very possible: they just adjusted a quantity), the barcode's
  // digits must NOT be read as a hand-typed replacement quantity, or a scan
  // silently overwrites the qty instead of adding the new item. So digits
  // here are mirrored into the same fast-burst buffer the scanner path
  // below uses, and Enter checks that buffer's length to tell the two
  // apart: a human typing a 1-3 digit quantity never produces a 6+ digit
  // burst at scanner speed, so that length is what actually distinguishes
  // them, not which element happened to have focus.
  const lineEl = document.activeElement && document.activeElement.closest
    && document.activeElement.closest('.line');
  if (lineEl) {
    const pid = Number(lineEl.dataset.pid);
    if (/^[0-9]$/.test(e.key)) {
      e.preventDefault();
      qtyKeyDigit(pid, e.key);
      scanBuf += e.key;
      clearTimeout(scanTimer);
      scanTimer = setTimeout(() => { scanBuf = ''; }, 120);
      return;
    }
    if (e.key === 'Backspace' || e.key === 'Delete') {
      e.preventDefault(); qtyKeyBackspace(pid);
      scanBuf = ''; // an edit, not a scan — whatever was building is stale
      return;
    }
    if (e.key === 'ArrowRight') { e.preventDefault(); adjustQty(pid, +1); return; }
    if (e.key === 'ArrowLeft') { e.preventDefault(); adjustQty(pid, -1); return; }
    if (e.key === 'ArrowUp') { e.preventDefault(); focusAdjacentLine(lineEl, -1); return; }
    if (e.key === 'ArrowDown') { e.preventDefault(); focusAdjacentLine(lineEl, +1); return; }
    if (e.key === 'Enter') {
      e.preventDefault();
      if (scanBuf.length >= SCAN_MIN_LEN) {
        // That was a scan, not a hand-typed quantity: abandon the pending
        // qty edit without committing it, and handle the scan normally.
        const code = scanBuf; scanBuf = '';
        S.qtyEdit = { pid: null, buf: '', timer: null };
        renderCart();
        onScan(code);
      } else {
        scanBuf = '';
        qtyCommit();
      }
      return;
    }
    return; // never fall through to the scanner buffer while a row is focused
  }

  // ------------------------------------------------------- scanner capture
  if (e.key === 'Enter') {
    if (scanBuf.length >= SCAN_MIN_LEN) { e.preventDefault(); onScan(scanBuf); }
    scanBuf = ''; return;
  }
  if (/^[0-9]$/.test(e.key)) {
    scanBuf += e.key;
    clearTimeout(scanTimer);
    scanTimer = setTimeout(() => { scanBuf = ''; }, 120);
  } else { scanBuf = ''; }
});

async function onScan(code) {
  if (!S.session) return;
  let p;
  try { p = await api('/api/scan?code=' + encodeURIComponent(code)); }
  catch (err) {
    // 401 is already handled by api() -> sessionLost(), which shows its own
    // message and returns to the login screen. Don't stack a second toast
    // that blames the scanner for it.
    if (err.status !== 401) toast(err.status === 404 ? 'Código no reconocido: ' + code : 'Error al leer', true);
    return;
  }
  // The scanner is a keyboard wedge: if the cashier left the caret in the
  // search box, the barcode's digits landed there too. Ringing it up while
  // the grid still shows "sin resultados" for a code nobody typed on purpose
  // is confusing, so clear it once the scan has been handled.
  if (S.search) setSearch('');
  if (S.checking) { showPrice(p); setChecking(false); return; }
  addToCart(p.id); toast(p.name);
}

/* -------------------------------------------------------------------- cart */
function addToCart(pid) {
  const p = S.byId[pid]; if (!p) return;
  const hit = S.cart.find(l => l.id === pid);
  if (hit) hit.qty++; else S.cart.push({ id: pid, name: p.name, price: p.price_cents, qty: 1 });
  renderCart();
}
function bump(pid, by) {
  const l = S.cart.find(x => x.id === pid); if (!l) return;
  l.qty += by; if (l.qty <= 0) S.cart = S.cart.filter(x => x.id !== pid);
  renderCart();
}
const cartTotal = () => S.cart.reduce((s, l) => s + l.price * l.qty, 0);

document.addEventListener('focusout', e => {
  if (!e.target.classList || !e.target.classList.contains('line')) return;
  setTimeout(() => {
    const a = document.activeElement;
    const stillOnSameRow = a && a.classList && a.classList.contains('line')
      && Number(a.dataset.pid) === S.qtyEdit.pid;
    if (!stillOnSameRow && S.qtyEdit.pid !== null) qtyCommit();
  }, 0);
});

/* ------------------------------------------------------ cart qty editing
   A focused cart row is a spinner: ArrowUp/ArrowDown nudge by one (same
   delta the on-screen +/- buttons apply), digits type an absolute
   replacement quantity with a debounced "lock-in", Enter commits early,
   and leaving the row (blur) always flushes whatever was pending rather
   than silently discarding typed digits. */
const QTY_EDIT_DEBOUNCE_MS = 900;
const QTY_EDIT_MAX_DIGITS = 3; // matches the server's qty <= 999

function qtyCommit() {
  const { pid, buf, timer } = S.qtyEdit;
  if (timer) clearTimeout(timer);
  S.qtyEdit = { pid: null, buf: '', timer: null };
  if (pid === null || buf === '') { renderCart(); return; } // nothing typed — leave qty as-is
  const n = parseInt(buf, 10);
  const l = S.cart.find(x => x.id === pid);
  if (!l) { renderCart(); return; }
  if (n <= 0) { S.cart = S.cart.filter(x => x.id !== pid); }       // explicit "0" removes the line
  else { l.qty = Math.min(n, 999); }
  renderCart();
}
function qtyKeyDigit(pid, digit) {
  if (S.qtyEdit.pid !== pid) { S.qtyEdit = { pid, buf: '', timer: null }; }
  if (S.qtyEdit.buf.length < QTY_EDIT_MAX_DIGITS) { S.qtyEdit.buf += digit; }
  clearTimeout(S.qtyEdit.timer);
  S.qtyEdit.timer = setTimeout(qtyCommit, QTY_EDIT_DEBOUNCE_MS);
  renderCart();
}
function qtyKeyBackspace(pid) {
  if (S.qtyEdit.pid !== pid) return;
  S.qtyEdit.buf = S.qtyEdit.buf.slice(0, -1);
  clearTimeout(S.qtyEdit.timer);
  S.qtyEdit.timer = setTimeout(qtyCommit, QTY_EDIT_DEBOUNCE_MS);
  renderCart();
}
function adjustQty(pid, delta) {
  // A pending typed value takes priority: nudging with Left/Right or the
  // on-screen +/- buttons commits it first, so "type 5, then press Right"
  // gives 6 rather than silently discarding the 5.
  if (S.qtyEdit.pid === pid) qtyCommit();
  bump(pid, delta);
}
function focusAdjacentLine(current, delta) {
  // Moving focus blurs `current`; the delegated focusout handler below
  // notices activeElement landed on a different row and flushes whatever
  // was pending there, so no explicit commit is needed here.
  const rows = $$('#lines .line');
  const next = rows[rows.indexOf(current) + delta];
  if (next) next.focus();
}

/* --------------------------------------------------------- roving focus
   Tabbing through 40+ product tiles one at a time to reach the last one is
   unusable. Arrow keys move a single "current" stop within the group
   instead; Tab still leaves the group in one step, same as any listbox. */
function makeRoving(container, itemSelector, { grid } = {}) {
  function items() { return Array.from(container.querySelectorAll(itemSelector)); }
  function columns() {
    if (!grid) return 1;
    const style = getComputedStyle(container);
    return style.gridTemplateColumns.split(' ').filter(Boolean).length || 1;
  }
  function sync() {
    const list = items();
    let current = list.findIndex(el => el.tabIndex === 0);
    if (current < 0) current = 0;
    list.forEach((el, i) => { el.tabIndex = i === current ? 0 : -1; });
  }
  // Every render* function rebuilds its children and calls makeRoving again,
  // but the container element itself survives -- so attaching here twice
  // leaves two live keydown handlers on it. preventDefault does not stop the
  // second one: it re-reads document.activeElement, already moved by the
  // first, and steps again. N renders meant N steps per keypress, silently
  // skipping rows (the user list after two clicks, the tile grid after two
  // category switches). Bind once; later calls only re-sync tabindex.
  if (!container.dataset.roving) {
    container.dataset.roving = '1';
    container.addEventListener('keydown', e => {
      const list = items();
      if (!list.length) return;
      let i = list.findIndex(el => el === document.activeElement);
      if (i < 0) return;
      const cols = columns();
      let next = null;
      if (e.key === 'ArrowRight') next = Math.min(i + 1, list.length - 1);
      else if (e.key === 'ArrowLeft') next = Math.max(i - 1, 0);
      else if (e.key === 'ArrowDown') next = Math.min(i + cols, list.length - 1);
      else if (e.key === 'ArrowUp') next = Math.max(i - cols, 0);
      if (next !== null && next !== i) {
        e.preventDefault();
        list[i].tabIndex = -1; list[next].tabIndex = 0; list[next].focus();
      }
    });
  }
  sync();
  return sync;
}
let syncCatsRoving = () => {}, syncGridRoving = () => {};

/* ------------------------------------------------ focus across re-renders
   Every render* below wipes its container and rebuilds the children, which
   destroys whatever node the keyboard user was standing on. Left unhandled
   that drops focus to <body>, and the arrow keys go dead with it: the roving
   listener lives on the container and only fires while focus is inside it.
   That is what made choosing a category feel clunky -- Enter re-rendered the
   strip out from under the user and they had to reach for the mouse.

   Wrap a rebuild in this and focus lands back where it was: by identity when
   the rows carry a stable key, otherwise by position, so removing the focused
   row lands on its neighbour rather than nowhere.

   renderCart() keeps its own copy of this logic -- it has an extra fallback
   for the cart emptying out entirely -- and is deliberately left as is. */
function keepFocus(box, rebuild, keyOf) {
  const active = document.activeElement;
  const kids = () => Array.from(box.children);
  // The focused node may be nested (a button inside a row), so anchor on the
  // direct child that contains it -- that is what the rebuild replaces.
  const owner = (box.contains(active) && active !== box)
    ? kids().find(el => el === active || el.contains(active)) : null;
  const prevKey = owner && keyOf ? keyOf(owner) : null;
  const prevIndex = owner ? kids().indexOf(owner) : -1;

  rebuild();
  if (!owner) return;

  const list = kids();
  if (!list.length) return;
  const target = (prevKey != null && keyOf ? list.find(el => keyOf(el) === prevKey) : null)
    || list[Math.min(prevIndex, list.length - 1)];
  if (!target) return;
  // In a roving group the target IS the item, but sync() has just parked the
  // single tab stop on the first child -- so the target is sitting at
  // tabindex="-1" and must be made the stop BEFORE focusing. Doing it the
  // other way round silently focuses nothing.
  if (box.dataset.roving) {
    list.forEach(el => { if (el !== target) el.tabIndex = -1; });
    target.tabIndex = 0;
    target.focus();
    return;
  }
  // Plain list: every row is its own tab stop, so leave tabindex alone --
  // rewriting it would break Tab navigation instead of smoothing it.
  const focusable = target.tabIndex >= 0 ? target : target.querySelector('[tabindex], button');
  if (focusable) focusable.focus();
}

/* -------------------------------------------------------- category order
   Two pinned entries, then everything else A-Z: 'Todos los productos' first
   as the escape hatch when something is filed in a category nobody can guess,
   then 'Frecuentes'. Both are pinned rather than sorted so their position
   never moves when the catalogue changes centrally -- muscle memory for the
   two most-used entries is worth more than alphabetical purity.

   'Todos' is virtual -- it exists only here, not in the category table.
   Creating it centrally would give it a sort_order to fight over and make it
   selectable as a product's own category in the admin panel, which is wrong:
   it is a view, not a place to file things. */
const ALL_CAT = '__all__';
function orderedCats() {
  const frec = S.cats.filter(c => c.id === 'frecuentes');
  const rest = S.cats.filter(c => c.id !== 'frecuentes')
    .sort((a, b) => a.name.localeCompare(b.name, 'es', { sensitivity: 'base' }));
  return [{ id: ALL_CAT, name: 'Todos los productos' }, ...frec, ...rest];
}
// Accent- and case-insensitive: a cashier typing "jabon" must find "Jabón",
// and the keyboard has no easy path to the accent anyway.
const fold = s => (s || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();

/* ------------------------------------------------------------------ render */
function renderCats() {
  const box = $('#cats');
  // Choosing a category calls straight back into here, so the rebuild destroys
  // the very button the user is standing on. keepFocus puts them back.
  keepFocus(box, () => {
    box.innerHTML = '';
    orderedCats().forEach(c => {
      const b = document.createElement('button');
      // While a search is running the grid is not showing any one category,
      // so highlighting one would be a lie about what is on screen.
      b.textContent = c.name; b.className = (!S.search && c.id === S.cat) ? 'on' : '';
      b.dataset.cid = String(c.id);
      // Switching category filters the grid only. The cart is untouched.
      b.onclick = () => { S.cat = c.id; setSearch(''); };
      box.appendChild(b);
    });
    syncCatsRoving = makeRoving(box, 'button');
  }, el => el.dataset.cid);
}
// Tiles read A-Z, same as the category list. The server still returns them by
// sort_hint (sales volume) DESC -- that ordering is left alone so this is a
// one-line revert, and so nothing server-side has to change.
//
// Sorts a copy: for 'Todos los productos' the filter chain hands back
// S.products itself, and sorting in place would permanently reorder the
// catalogue every other view reads from.
const byName = list => list.slice()
  .sort((a, b) => a.name.localeCompare(b.name, 'es', { sensitivity: 'base' }));

function gridProducts() {
  // A search spans the whole catalogue, not the selected category: the point
  // of it is finding something you cannot place, so filtering it down to the
  // category you already guessed wrong would defeat it.
  if (S.search) {
    const q = fold(S.search);
    return byName(S.products.filter(p => fold(p.name).includes(q)));
  }
  if (S.cat === ALL_CAT) return byName(S.products);
  if (S.cat === 'frecuentes') return byName(S.products.filter(p => p.is_frequent));
  return byName(S.products.filter(p => p.category_id === S.cat));
}
function setSearch(v) {
  S.search = v;
  if ($('#prodSearch').value !== v) $('#prodSearch').value = v;
  $('#prodSearchClear').classList.toggle('hidden', !v);
  renderCats(); renderGrid();
}
function renderGrid() {
  const list = gridProducts();
  const box = $('#grid');
  // Keyed by product id: a re-render that leaves the product on screen keeps
  // the user on that exact tile, even if the ordering shifted around it.
  keepFocus(box, () => {
    box.innerHTML = '';
    if (!list.length && S.search) {
      const d = document.createElement('div');
      d.className = 'muted';
      d.style.cssText = 'grid-column:1/-1;padding:8px 2px;font-size:14px';
      d.textContent = 'Sin resultados para «' + S.search + '»';
      box.appendChild(d);
    }
    list.forEach(p => {
      const b = document.createElement('button');
      b.className = 'tile';
      b.dataset.pid = String(p.id);
      b.innerHTML = `<span class="n"></span><span class="p num">${mxn(p.price_cents)}</span>`;
      b.querySelector('.n').textContent = p.name;
      b.onclick = () => S.checking ? (showPrice(p), setChecking(false)) : addToCart(p.id);
      box.appendChild(b);
    });
    syncGridRoving = makeRoving(box, '.tile', { grid: true });
  }, el => el.dataset.pid);
}
function renderCart() {
  const box = $('#lines');
  // Rebuilding the list destroys whatever row was focused, same trap fixed
  // elsewhere for overlays. Remember it (by product id, not DOM reference)
  // and restore it below — or land on the nearest remaining row if that
  // product just got removed, so a keyboard user never loses their place.
  const active = document.activeElement;
  const focusedPid = active && active.classList && active.classList.contains('line')
    ? Number(active.dataset.pid) : null;
  const focusedIndex = focusedPid !== null ? S.cart.findIndex(l => l.id === focusedPid) : -1;

  box.innerHTML = '';
  if (!S.cart.length) {
    box.innerHTML = '<div class="empty">Escanea un producto<br>o tócalo en la lista</div>';
  }
  S.cart.forEach(l => {
    const editing = S.qtyEdit.pid === l.id;
    const qtyLabel = editing ? (S.qtyEdit.buf || '–') : String(l.qty);

    const d = document.createElement('div');
    d.className = 'line'; d.tabIndex = 0; d.dataset.pid = String(l.id);
    d.innerHTML = `<div class="g"><div class="nm"></div><div class="ea num">${mxn(l.price)} c/u</div></div>
      <div style="display:flex;align-items:center;gap:6px">
        <button class="qbtn" data-d="-1" tabindex="-1">−</button>
        <span class="num qty${editing ? ' editing' : ''}" style="min-width:26px;text-align:center;font-size:15px;font-weight:700">${qtyLabel}</span>
        <button class="qbtn" data-d="1" tabindex="-1">+</button></div>
      <span class="tt num">${mxn(l.price * l.qty)}</span>`;
    d.querySelector('.nm').textContent = l.name;
    d.querySelectorAll('.qbtn').forEach(b => b.onclick = () => adjustQty(l.id, +b.dataset.d));
    box.appendChild(d);
  });

  if (focusedPid !== null) {
    const rows = $$('#lines .line');
    const same = rows.find(r => Number(r.dataset.pid) === focusedPid);
    if (same) same.focus();
    else if (rows.length) rows[Math.min(focusedIndex, rows.length - 1)].focus();
    else { const anchor = sellScreenAnchor(); if (anchor) anchor.focus(); }
  }
  const n = S.cart.reduce((s, l) => s + l.qty, 0);
  $('#count').textContent = n === 1 ? '1 artículo' : n + ' artículos';
  $('#total').textContent = mxn(cartTotal());
  $('#cobrar').disabled = !S.cart.length;
  // Every path that changes the cart ends here, so this is the one hook that
  // cannot be forgotten when a new one is added. An empty cart removes the
  // saved copy, which is what makes a cancel or a completed sale clear it for
  // free.
  saveCart();
}
function renderWho() {
  $('#closeShiftBtn').classList.toggle('hidden', !(S.session && S.shift));
  // Visible to cashiers too. A cashier gets in by an admin typing their PIN
  // (see the click handler), which is the whole point: reaching the panel used
  // to mean logging out, and logging out ends the shift.
  $('#adminLink').classList.toggle('hidden', !S.session);
  if (!S.session) { $('#who').innerHTML = ''; return; }
  const ini = S.session.name.split(' ').map(w => w[0]).slice(0, 2).join('');
  $('#who').innerHTML = `<div class="avatar">${ini}</div><div style="line-height:1.15">
    <div style="font-size:13px;font-weight:600">${S.session.name}</div>
    <div class="muted" style="font-size:11px">${S.shift ? 'Turno abierto' : 'Sin turno'}</div></div>`;
}

/* ------------------------------------------------------------- price check */
function setChecking(on) {
  S.checking = on;
  $('#toolbar').classList.toggle('checking', on);
  $('#checkBtn').textContent = on ? 'Cancelar consulta' : 'Consultar precio';
  $('#toolbarHint').textContent = on ? 'Escanea o toca un producto' : 'No modifica la venta en curso';
}
function showPrice(p) {
  S.pcProduct = p;
  $('#pcName').textContent = p.name;
  $('#pcPrice').textContent = mxn(p.price_cents);
  $('#pcSub').textContent = 'La venta en curso sigue intacta';
  openOverlay('#priceOverlay');
  $('#pcAdd').focus();
}
function closePriceCheck() { closeOverlay('#priceOverlay'); }

/* ------------------------------------------------------------------ keypads
   Each numeric pad is built once with on-screen buttons; the same onKey
   function is also what the physical-keyboard router calls, so touch and
   keyboard entry are always identical. */
function keypad(el, onKey, extra) {
  el.innerHTML = '';
  ['1','2','3','4','5','6','7','8','9', extra || '', '0', '←'].forEach(k => {
    const b = document.createElement('button');
    b.textContent = k;
    if (k === '') { b.style.visibility = 'hidden'; b.tabIndex = -1; }
    else { b.onclick = () => onKey(k); b.tabIndex = -1; } // reachable via keyboard entry, not Tab
    el.appendChild(b);
  });
}
function dots(el, len, filled, cls) {
  el.innerHTML = '';
  for (let i = 0; i < len; i++) {
    const d = document.createElement('div');
    d.className = 'dot' + (i < filled ? ' on' : '');
    if (cls && i < filled) { d.style.background = cls; d.style.borderColor = cls; }
    el.appendChild(d);
  }
}

/* -------------------------------------------------------------------- login */
function renderUsers() {
  const box = $('#userList');
  // Selecting a user re-renders this list, which used to drop focus and kill
  // the arrow keys until Escape put it back. Now Enter keeps your place.
  keepFocus(box, () => {
    box.innerHTML = '';
    S.users.forEach(u => {
      const admin = u.role === 'admin', tint = admin ? 'var(--amber)' : 'var(--green)';
      const b = document.createElement('button');
      b.style.cssText = 'display:flex;align-items:center;gap:13px;padding:12px 14px;border-radius:10px;'
        + 'text-align:left;background:' + (S.pinUser === u.id ? 'var(--panel2)' : 'var(--panel)')
        + ';border:1px solid ' + (S.pinUser === u.id ? tint : '#2a3543');

      const av = document.createElement('span');
      av.style.cssText = 'width:42px;height:42px;border-radius:50%;flex:0 0 auto;color:#0d1319;'
        + 'display:grid;place-items:center;font-weight:700;font-size:14px;background:' + tint;
      av.textContent = u.name.split(' ').map(w => w[0]).slice(0, 2).join('');

      const nm = document.createElement('span');
      nm.style.cssText = 'display:block;font-size:16px;font-weight:600';
      nm.textContent = u.name;
      const rl = document.createElement('span');
      rl.className = 'muted';
      rl.style.cssText = 'display:block;font-size:12.5px;margin-top:1px';
      rl.textContent = admin ? 'Administrador' : 'Cajera/o';

      const txt = document.createElement('span');
      txt.append(nm, rl);
      b.append(av, txt);
      b.dataset.uid = String(u.id);
      b.onclick = () => { S.pinUser = u.id; S.pin = ''; renderUsers(); renderPin(); };
      box.appendChild(b);
    });
    makeRoving(box, 'button');
  }, el => el.dataset.uid);
}
function pinLen() {
  const u = S.users.find(x => x.id === S.pinUser);
  return u && u.role === 'admin' ? 6 : 4;
}
function renderPin() {
  const u = S.users.find(x => x.id === S.pinUser);
  $('#pinPrompt').textContent = u ? 'PIN de ' + u.name.split(' ')[0] : 'Selecciona tu nombre';
  $('#pinSub').textContent = u ? (pinLen() === 6 ? '6 dígitos · administrador' : '4 dígitos')
                               : 'Cada venta queda registrada a tu nombre';
  dots($('#pinDots'), pinLen(), S.pin.length, u && u.role === 'admin' ? 'var(--amber)' : null);
  // A user is always "selected" once chosen, keyboard or not — arm the pad.
  S.activeKeypad = u ? pinKey : null;
}
async function pinKey(k) {
  if (!S.pinUser) return;
  $('#pinErr').textContent = '';
  if (k === '←') { S.pin = S.pin.slice(0, -1); renderPin(); return; }
  S.pin += k; renderPin();
  if (S.pin.length < pinLen()) return;
  try {
    const r = await api('/api/login', { method: 'POST', body: JSON.stringify({ user_id: S.pinUser, pin: S.pin }) });
    S.session = r.session; S.pin = ''; S.pinUser = null;
    closeOverlay('#loginOverlay');
    renderWho(); await afterLogin();
  } catch (e) {
    S.pin = ''; renderPin();
    $('#pinErr').textContent = e.message === 'locked' ? 'Bloqueado 5 minutos por intentos fallidos'
                             : e.message === 'bad_pin' ? 'PIN incorrecto' : 'No se pudo entrar';
    // A wrong PIN doesn't change S.pinUser, but the button that represents
    // them may no longer be focused (or never was, if entry came from a
    // scan-speed retry) — without this, a keyboard-only retry has no anchor
    // and Up/Down (which need focus inside #userList to fire at all) do nothing.
    const idx = S.users.findIndex(u => u.id === S.pinUser);
    const btn = $$('#userList button')[idx];
    if (btn) btn.focus();
  }
}

/* -------------------------------------------------------------------- shift */
async function afterLogin() {
  const b = await api('/api/bootstrap');
  S.shift = b.shift; renderWho();
  if (!S.shift) {
    S.float = '';
    renderFloat();
    openOverlay('#shiftOverlay', k => {
      if (k === '←') S.float = S.float.slice(0, -1); else if (S.float.length < 6) S.float += k;
      renderFloat();
    });
  } else {
    // A shift was already running, so this may be a cashier coming back after
    // the service restarted under them. Offer their cart back before focusing.
    restoreCart();
    // No overlay opens in this branch (shift already running), so nothing
    // else would claim focus — same failure as the disabled-button case:
    // it falls back to <body> and a keyboard user has no visible anchor.
    const anchor = sellScreenAnchor();
    if (anchor) anchor.focus();
  }
}
function renderFloat() { $('#floatVal').textContent = mxn(parseInt(S.float || '0', 10) * 100); }

/* ------------------------------------------------------------------ payment */
function renderPay() {
  const total = cartTotal(), tend = Math.round(parseFloat(S.tendered || '0') * 100);
  const change = tend - total, ok = S.tendered !== '' && change >= 0;
  $('#tendered').textContent = S.tendered === '' ? '$0.00' : mxn(tend);
  $('#payTotal').textContent = mxn(total);
  $('#change').textContent = mxn(ok ? change : 0);
  $('#change').style.color = ok ? 'var(--green)' : 'var(--faint)';
  $('#changeBox').style.borderColor = ok ? '#2c5f45' : '#33414f';
  $('#changeHint').textContent = S.tendered === '' ? 'Captura el efectivo recibido'
      : ok ? 'Entregar al cliente' : 'Faltan ' + mxn(total - tend);
  $('#confirmPay').disabled = !ok;
}
function openPay() {
  S.tendered = ''; renderPay();
  openOverlay('#payOverlay', k => {
    if (k === '←') S.tendered = S.tendered.slice(0, -1);
    else if (k === '.') { if (!S.tendered.includes('.')) S.tendered += '.'; }
    else S.tendered += k;
    renderPay();
  });
}
function closePay() { closeOverlay('#payOverlay'); }

/* ----------------------------------------------------------------- override
   onOk(pin) is awaited and must itself verify the PIN server-side — either
   directly (guarded actions with no other endpoint to call, like cancelling
   a sale) or as a side effect of the real action's own endpoint (retiro,
   shift close: both reject a bad admin_pin with 403 'override_denied').
   Throwing shows the message inline and leaves the overlay open to retry,
   instead of the previous behaviour where any six digits were trusted. */
function askOverride(what, onOk) {
  S.ovr = { what, onOk }; S.ovrPin = '';
  $('#ovrWhat').textContent = what; $('#ovrErr').textContent = '';
  dots($('#ovrDots'), 6, 0, 'var(--amber)');
  openOverlay('#ovrOverlay', async k => {
    $('#ovrErr').textContent = '';
    if (k === '←') {
      S.ovrPin = S.ovrPin.slice(0, -1);
      dots($('#ovrDots'), 6, S.ovrPin.length, 'var(--amber)');
      return;
    }
    if (S.ovrPin.length >= 6) return;
    S.ovrPin += k;
    dots($('#ovrDots'), 6, S.ovrPin.length, 'var(--amber)');
    if (S.ovrPin.length !== 6) return;
    const p = S.ovrPin; S.ovrPin = '';
    try {
      await S.ovr.onOk(p);
    } catch (e) {
      dots($('#ovrDots'), 6, 0, 'var(--amber)');
      $('#ovrErr').textContent = e.message === 'override_denied' ? 'PIN incorrecto'
                                : (e.message || 'No se pudo autorizar');
    }
  });
}
function closeOverride() { closeOverlay('#ovrOverlay'); }

/* ------------------------------------------------------- retiro parcial */
function renderDrop() { $('#dropVal').textContent = mxn(parseInt(S.dropAmount || '0', 10) * 100); $('#dropConfirm').disabled = !S.dropAmount || parseInt(S.dropAmount, 10) <= 0; }
async function openDrawer(reason, quiet) {
  // No confirmation step: the whole point is one keypress mid-sale while the
  // cashier's other hand is holding change. The server audits every attempt,
  // tagged with why -- a drawer opened to count at close reads very
  // differently in the log from one opened for no stated reason.
  try {
    await api('/api/drawer/open', { method: 'POST',
      body: JSON.stringify({ reason: reason || 'manual' }) });
    if (!quiet) toast('Cajon abierto');
  } catch (e) {
    toast(e.message === 'no_printer_node' ? 'Sin impresora: el cajon no responde'
                                          : 'No se pudo abrir el cajon');
  }
}
function openDrop() {
  // Open the drawer FIRST and ask afterwards. The cashier is mid-service with
  // a customer waiting; making them answer "how much?" before the drawer will
  // open adds a pause to every retiro for no benefit. The amount is a record
  // of something they are about to do physically, not an authorisation for it
  // -- and the opening itself is audited either way, so dismissing this
  // dialogue is a legitimate outcome, not a hole.
  openDrawer('retiro', true);
  S.dropAmount = ''; renderDrop();
  openOverlay('#dropOverlay', k => {
    if (k === '←') S.dropAmount = S.dropAmount.slice(0, -1);
    else if (S.dropAmount.length < 6) S.dropAmount += k;
    renderDrop();
  });
}
function closeDrop() { closeOverlay('#dropOverlay'); }

/* --------------------------------------------------- agregar efectivo
   Cash going IN: the mirror of a retiro, and it follows the same shape on
   purpose. Drawer first, amount afterwards -- the cashier is standing there
   with notes in hand, and the record is of something they are about to do
   physically, not permission to do it. Dismissing without an amount is a
   legitimate outcome; the opening is audited either way. */
function renderAddCash() {
  $('#addCashVal').textContent = mxn(parseInt(S.addCash || '0', 10) * 100);
  $('#addCashConfirm').disabled = !S.addCash || parseInt(S.addCash, 10) <= 0;
}
function openAddCash() {
  openDrawer('efectivo', true);
  S.addCash = ''; renderAddCash();
  openOverlay('#addCashOverlay', k => {
    if (k === '←') S.addCash = S.addCash.slice(0, -1);
    else if (S.addCash.length < 6) S.addCash += k;
    renderAddCash();
  });
}
function closeAddCash() { closeOverlay('#addCashOverlay'); }

/* --------------------------------------------------------- shift close */
function renderShiftClose() {
  const sum = S.shiftSummary; if (!sum) return;
  const counted = parseInt(S.closeCash || '0', 10) * 100;
  const hasInput = S.closeCash !== '';
  const diff = counted - sum.expected_cents;
  const shortfall = hasInput && diff < SHORTFALL_REQUIRES_ADMIN_CENTS;

  $('#closeCashVal').textContent = hasInput ? mxn(counted) : '$0.00';
  $('#closeFloat').textContent = mxn(sum.opening_float_cents);
  $('#closeSales').textContent = mxn(sum.sales_cents);
  $('#closeDrops').textContent = (sum.drops_cents ? '−' : '') + mxn(sum.drops_cents);
  $('#closeExpected').textContent = mxn(sum.expected_cents);
  $('#closeDiff').textContent = hasInput ? mxn(diff) : '$0.00';
  $('#closeDiff').style.color = !hasInput ? 'var(--faint)' : diff === 0 ? 'var(--green)'
    : diff < 0 ? 'var(--red)' : 'var(--amber)';
  $('#closeDiffBox').style.borderColor = !hasInput ? '#33414f' : diff === 0 ? '#2c5f45'
    : diff < 0 ? '#5f2c2c' : '#5f4a2c';
  $('#closeDiffHint').textContent = !hasInput ? 'Captura el efectivo contado'
    : diff === 0 ? 'Cuadra exacto' : diff < 0 ? 'Faltante' : 'Sobrante';
  $('#closeWarn').classList.toggle('hidden', !shortfall);
  $('#closeConfirm').disabled = !hasInput;
}
function askCloseShift() {
  // A shift close ends the session, prints the corte and cannot be undone, so
  // it gets a confirmation -- the button sits in the header next to everyday
  // controls and is easy to hit by accident.
  openOverlay('#closeConfirmOverlay');
}
function cancelCloseShift() { closeOverlay('#closeConfirmOverlay'); }

function askCancelSale() {
  // Name what is about to be lost. "Are you sure?" with nothing behind it gets
  // dismissed on reflex; "3 artículos · $85.00" is a number the cashier can
  // check against the basket in front of them.
  const n = S.cart.reduce((a, l) => a + l.qty, 0);
  $('#cancelWhat').textContent =
    `${n} artículo${n === 1 ? '' : 's'} · ${mxn(cartTotal())}`;
  openOverlay('#cancelConfirmOverlay');
}
function doCancelSale() {
  closeOverlay('#cancelConfirmOverlay');
  S.cart = []; renderCart(); toast('Venta cancelada');
}

async function openShiftClose() {
  // The drawer has to be open before anyone can count what is in it, so this
  // fires as part of starting the count rather than making the cashier press
  // a second key. Best-effort: a drawer that will not open must not block the
  // close, since the cashier can still open it with the key and count.
  openDrawer('shift_close', true);
  try { S.shiftSummary = await api('/api/shift/summary'); }
  catch (e) { toast('No se pudo cargar el turno: ' + e.message, true); return; }
  S.closeCash = '';
  openOverlay('#shiftCloseOverlay', k => {
    if (k === '←') S.closeCash = S.closeCash.slice(0, -1);
    else if (S.closeCash.length < 6) S.closeCash += k;
    renderShiftClose();
  });
  renderShiftClose();
}
function closeShiftClose() { closeOverlay('#shiftCloseOverlay'); }

/* ----------------------------------------------------------------- logout */
async function logout() {
  try { await api('/api/logout', { method: 'POST' }); } catch (e) { /* best-effort */ }
  // Deliberate exit — unlike sessionLost(), nothing here is meant to come back.
  clearSavedCart();
  S.session = null; S.shift = null; S.cart = []; S.pin = ''; S.pinUser = null;
  S.shiftSummary = null;
  renderCart(); renderWho();
  openOverlay('#loginOverlay');
  renderUsers(); renderPin();
}

/* ----------------------------------------------------------------- devices
   Polled rather than assumed. The old header claimed the scanner was ready
   whether or not it was plugged in, which trains people to ignore it. */
const DEV_COLOR = { ok: 'var(--green)', blocked: 'var(--amber)', missing: 'var(--red)' };

function setTestMode(on) {
  // A silent test mode is a trap: the cashier presses COBRAR, no ticket comes
  // out, and they assume the printer is broken. Keep it loud and permanent on
  // screen for as long as it is on.
  S.testMode = !!on;
  $('#testPill').classList.toggle('hidden', !S.testMode);
}
function renderDevices(d) {
  if (!d) return;
  const set = (el, label, st) => {
    el.innerHTML = '';
    const dot = document.createElement('span');
    dot.style.cssText = 'width:8px;height:8px;border-radius:50%;flex:0 0 auto;background:' +
                        (DEV_COLOR[st.state] || 'var(--faint)');
    const txt = document.createElement('span');
    txt.textContent = label + ': ' + st.note;
    el.append(dot, txt);
    el.style.borderColor = st.state === 'ok' ? 'var(--line)' : DEV_COLOR[st.state];
    el.style.color = st.state === 'ok' ? 'var(--dim)' : DEV_COLOR[st.state];
  };
  set($('#scanPill'), 'Escáner', d.scanner);
  set($('#printPill'), 'Impresora', d.printer);
}

async function pollDevices() {
  try {
    const d = await api('/api/devices');
    renderDevices(d);
    // Central can change prices while the till is open. Reload the catalogue
    // when the revision moves rather than making someone restart the kiosk --
    // a cashier charging last week's price is a real loss, not a cosmetic bug.
    if (S.catalogueRevision == null) S.catalogueRevision = d.catalogue_revision;
    else if (d.catalogue_revision !== S.catalogueRevision) {
      S.catalogueRevision = d.catalogue_revision;
      await reloadCatalogue();
    }
  } catch (e) { /* keep last known */ }
}

async function reloadCatalogue() {
  // Deliberately does NOT touch the cart. Lines already added keep the price
  // they were rung up at, which is what the customer was quoted; a silent
  // re-price mid-sale would be worse than a stale one.
  const b = await api('/api/bootstrap');
  S.cats = b.catalogue.categories;
  S.products = b.catalogue.products;
  S.byId = Object.fromEntries(S.products.map(p => [p.id, p]));
  if (S.cat !== ALL_CAT && !S.cats.some(c => c.id === S.cat)) S.cat = 'frecuentes';
  renderCats(); renderGrid();
  toast('Catálogo actualizado');
}
setInterval(pollDevices, 5000);

/* --------------------------------------------------------------------- boot */
async function boot() {
  updateInert();
  const b = await api('/api/bootstrap');
  S.users = b.users; S.cats = b.catalogue.categories; S.products = b.catalogue.products;
  S.byId = Object.fromEntries(S.products.map(p => [p.id, p]));
  S.session = b.session; S.shift = b.shift;
  setTestMode(b.test_mode);
  $('#outbox').textContent = b.outbox_pending ? b.outbox_pending + ' por sincronizar' : '';
  renderDevices(b.devices);
  renderUsers(); renderPin(); renderCats(); renderGrid(); renderCart(); renderWho();
  keypad($('#pinPad'), k => S.activeKeypad && S.activeKeypad(k));
  keypad($('#ovrPad'), k => S.activeKeypad && S.activeKeypad(k));
  keypad($('#floatPad'), k => S.activeKeypad && S.activeKeypad(k));
  keypad($('#payPad'), k => S.activeKeypad && S.activeKeypad(k), '.');
  keypad($('#dropPad'), k => S.activeKeypad && S.activeKeypad(k));
  keypad($('#addCashPad'), k => S.activeKeypad && S.activeKeypad(k));
  keypad($('#closePad'), k => S.activeKeypad && S.activeKeypad(k));
  [50, 100, 200, 500].forEach(v => {
    const b2 = document.createElement('button');
    b2.textContent = '$' + v; b2.style.cssText = 'height:48px;border-radius:8px;background:#2a3543;font-size:15px;font-weight:600';
    b2.tabIndex = -1;
    b2.onclick = () => { S.tendered = String(v); renderPay(); };
    $('#quick').appendChild(b2);
  });

  if (S.session) { closeOverlay('#loginOverlay'); await afterLogin(); }
  else { const first = $('#userList button'); if (first) first.focus(); }
}

/* ------------------------------------------------------------------ wiring */
$('#checkBtn').onclick = () => setChecking(!S.checking);
$('#prodSearch').addEventListener('input', e => setSearch(e.target.value));
$('#prodSearchClear').onclick = () => { setSearch(''); $('#prodSearch').focus(); };
$('#prodSearch').addEventListener('keydown', e => {
  // Escape clears the box, then gives the screen back. Left to bubble it would
  // hit the document handler, which on the sell screen only cancels a price
  // check -- so the search would survive an Escape that visibly did nothing.
  if (e.key === 'Escape') { e.stopPropagation(); setSearch(''); e.target.blur(); }
});
$('#pcClose').onclick = closePriceCheck;
$('#pcAdd').onclick = () => { addToCart(S.pcProduct.id); closePriceCheck(); };
$('#cobrar').onclick = openPay;
$('#cancelPay').onclick = closePay;
$('#ovrCancel').onclick = closeOverride;
/* Admin panel from a cashier account. An admin already navigates straight
   through; anyone else has to have an admin's PIN typed in first, and the
   server -- not this handler -- is what actually verifies it.

   Navigating away is safe now: the cart is mirrored to localStorage and comes
   back on return (see cart recovery above). Before that it would have silently
   destroyed a half-scanned sale, which is why this button waited for it. */
$('#adminLink').onclick = (e) => {
  if (!S.session) { e.preventDefault(); return; }
  if (S.session.role === 'admin') return;              // plain navigation
  e.preventDefault();
  askOverride('Abrir el panel de administración', async pin => {
    await api('/api/admin/elevate', { method: 'POST', body: JSON.stringify({ pin }) });
    closeOverride();
    location.href = '/admin';
  });
};

$('#dropCancel').onclick = closeDrop;
$('#dropDismiss').onclick = closeDrop;
$('#addCashCancel').onclick = closeAddCash;
$('#addCashDismiss').onclick = closeAddCash;
$('#closeCancel').onclick = closeShiftClose;
$('#closeShiftBtn').onclick = askCloseShift;
$('#cancelConfirmYes').onclick = doCancelSale;
$('#cancelConfirmNo').onclick = () => closeOverlay('#cancelConfirmOverlay');
$('#closeConfirmNo').onclick = cancelCloseShift;
$('#closeConfirmYes').onclick = () => { closeOverlay('#closeConfirmOverlay'); openShiftClose(); };

$('#openShift').onclick = async () => {
  await api('/api/shift/open', { method: 'POST',
    body: JSON.stringify({ opening_float_cents: parseInt(S.float || '0', 10) * 100 }) });
  closeOverlay('#shiftOverlay');
  S.shift = (await api('/api/bootstrap')).shift; renderWho();
  toast('Turno abierto');
};

$('#confirmPay').onclick = async () => {
  const tend = Math.round(parseFloat(S.tendered || '0') * 100);
  try {
    const r = await api('/api/sale', { method: 'POST', body: JSON.stringify({
      lines: S.cart.map(l => ({ product_id: l.id, qty: l.qty })), tendered_cents: tend }) });
    S.cart = []; S.tendered = '';
    closePay();
    renderCart();
    // The sale is committed either way -- these only affect what the cashier
    // needs to do next. A failed print means reach for a reprint; a failed
    // drawer means reach for the key. Saying nothing means finding out from
    // the customer.
    if (r.test_mode) toast(`Ticket #${r.seq} · cambio ${r.change} · PRUEBAS, sin ticket`);
    else if (r.printed === false && r.drawer === false)
      toast(`Ticket #${r.seq} · cambio ${r.change} · NO imprimio y el cajon no abrio`, true);
    else if (r.printed === false)
      toast(`Ticket #${r.seq} · cambio ${r.change} · NO se imprimio el ticket`, true);
    else if (r.drawer === false)
      toast(`Ticket #${r.seq} · cambio ${r.change} · el cajon no abrio`, true);
    else toast(`Ticket #${r.seq} · cambio ${r.change}`);
  } catch (e) { toast('No se pudo cobrar: ' + e.message, true); }
};

$$('#guarded button').forEach(b => b.onclick = () => {
  const act = b.dataset.act;
  if (act === 'cancel') {
    if (!S.cart.length) return;
    // Still no admin override: nothing is recorded server-side before COBRAR
    // and no money has moved, so this only empties a basket on screen --
    // exactly like putting the items back on the shelf. Requiring a manager
    // for it just trains people to keep a supervisor's PIN to hand, which is
    // worse for the things that genuinely need one.
    //
    // It does ask, though. Clearing a part-scanned basket by accident means
    // re-scanning it in front of the customer, and X on the macropad is one
    // key away from O. A confirmation is cheap; the re-scan is not.
    askCancelSale();
  } else if (act === 'drop') {
    openDrop();
  }
});

$('#dropConfirm').onclick = async () => {
  const amt = parseInt(S.dropAmount || '0', 10) * 100;
  if (amt <= 0) return;
  // No admin override. The drawer is already open at this point, so gating the
  // *record* behind a PIN only made the likely outcome "cash leaves with no
  // record while someone hunts for a manager". Both halves are logged: the
  // opening as drawer_opened/retiro, the amount as a cash_movement.
  try {
    const r = await api('/api/cash/drop', { method: 'POST',
      body: JSON.stringify({ amount_cents: amt }) });
    closeDrop();
    toast(`Retiro de ${mxn(amt)} registrado · sobre ${r.envelope_no}`);
  } catch (e) {
    toast('No se pudo registrar el retiro: ' + e.message, true);
  }
};

/* Two ways in, and they need different words and different exits:
     afterClose  the shift has just been closed -- the routine end of day
     fromLogin   nobody is signed in, which is how the till sits when idle and
                 is exactly the state someone reaches for the power button in */
let powerContext = 'afterClose';

function openPower(ctx) {
  powerContext = ctx;
  const fromLogin = ctx === 'fromLogin';
  $('#powerTitle').textContent = fromLogin ? 'Apagar la caja' : 'Turno cerrado';
  $('#powerSub').textContent = fromLogin ? '¿Seguro que quieres apagar la caja?'
                                         : '¿Quieres apagar la caja?';
  $('#powerSecondary').textContent = fromLogin ? 'Cancelar' : 'Solo cerrar sesión';
  const btn = $('#powerOff');
  btn.disabled = false; btn.textContent = 'Apagar la caja';
  openOverlay('#powerOverlay');
  // openOverlay() focuses the first enabled button, which here is the one that
  // powers the till off -- and a stray Enter would then take it down. Put
  // focus on the way OUT instead; shutting down should need a deliberate press.
  $('#powerSecondary').focus();
}

$('#loginPowerBtn').onclick = () => openPower('fromLogin');

$('#powerSecondary').onclick = async () => {
  closeOverlay('#powerOverlay');
  if (powerContext === 'fromLogin') {
    // closeOverlay() cleared activeKeypad; renderPin() re-arms the login pad,
    // otherwise the PIN keys go dead after cancelling.
    renderPin();
  } else {
    await logout();
  }
};

$('#powerOff').onclick = async () => {
  const btn = $('#powerOff');
  btn.disabled = true; btn.textContent = 'Apagando…';
  try {
    await api('/api/power', { method: 'POST', body: JSON.stringify({ mode: 'poweroff' }) });
    // Nothing to do but wait -- the machine goes down about a second and a
    // half from here. Leave the message on screen rather than logging out,
    // so the last thing anyone sees is what is actually happening.
  } catch (e) {
    btn.disabled = false; btn.textContent = 'Apagar la caja';
    toast(e.message === 'shift_open' ? 'Hay un turno abierto; ciérralo primero'
                                     : 'No se pudo apagar: ' + e.message, true);
  }
};

$('#addCashConfirm').onclick = async () => {
  const amt = parseInt(S.addCash || '0', 10) * 100;
  if (amt <= 0) return;
  try {
    await api('/api/cash/float_in', { method: 'POST',
      body: JSON.stringify({ amount_cents: amt }) });
    closeAddCash();
    toast(`Efectivo agregado: ${mxn(amt)}`);
  } catch (e) {
    toast('No se pudo registrar el efectivo: ' + e.message, true);
  }
};

$('#closeConfirm').onclick = () => {
  const sum = S.shiftSummary; if (!sum) return;
  const counted = parseInt(S.closeCash || '0', 10) * 100;
  const diff = counted - sum.expected_cents;

  const doClose = adminPin => {
    const body = { counted_cents: counted };
    if (adminPin) body.admin_pin = adminPin;
    return api('/api/shift/close', { method: 'POST', body: JSON.stringify(body) });
  };
  // Always hide whichever overlay is currently up BEFORE logout() opens the
  // login screen — otherwise both are briefly visible at once, since
  // logout() doesn't know to close overlays it didn't open itself.
  const finishAndLogout = async (r, hideFirst) => {
    hideFirst();
    // The corte is the piece that goes in the envelope with the cash, so a
    // failed print is worth saying out loud -- the shift is closed either way
    // and the numbers are recoverable from the admin panel.
    toast(r.printed === false ? `Turno cerrado · diferencia ${r.difference} · NO se imprimio el corte`
                              : `Turno cerrado · diferencia ${r.difference}`,
          r.printed === false);
    // Offer to power down here rather than logging straight out. Closing the
    // shift IS closing up, and this is the only moment we can be sure nothing
    // is in flight -- the server refuses to power off while a shift is open.
    // Until this existed the only way to turn the till off was the physical
    // button, and held down that cuts power in firmware.
    openPower('afterClose');
  };

  if (diff < SHORTFALL_REQUIRES_ADMIN_CENTS) {
    $('#shiftCloseOverlay').classList.add('hidden'); // same no-flicker handoff as retiro
    askOverride('Cerrar turno con faltante', async pin => {
      const r = await doClose(pin);
      await finishAndLogout(r, closeOverride);
    });
  } else {
    (async () => {
      try {
        const r = await doClose(null);
        await finishAndLogout(r, closeShiftClose);
      } catch (e) { toast('No se pudo cerrar el turno: ' + e.message, true); }
    })();
  }
};

boot().catch(e => {
  console.error(e);
  toast('Error al iniciar: ' + e.message, true);
  showFatalError('Error al iniciar', e);
});
