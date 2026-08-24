'use strict';

const S = { users: [], cats: [], products: [], byId: {}, cat: 'frecuentes',
            cart: [], session: null, shift: null, checking: false, pcProduct: null,
            pin: '', pinUser: null, ovr: null, ovrPin: '', tendered: '', float: '',
            activeKeypad: null,
            // Pending quantity edit on a focused cart row: typed digits build
            // `buf` as an ABSOLUTE replacement value (never appended to the
            // committed qty). A fresh digit arriving after the buffer has
            // already been committed (pid reset to null) starts a new buffer
            // instead of continuing the old one — typing "2","3" then, later,
            // "1","3" yields 13, not 2313.
            qtyEdit: { pid: null, buf: '', timer: null } };

const $  = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
const mxn = c => (c < 0 ? '−$' : '$') + Math.abs(c / 100).toLocaleString('es-MX',
                 { minimumFractionDigits: 2, maximumFractionDigits: 2 });

async function api(path, opts) {
  const r = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts));
  if (!r.ok) { const e = new Error((await r.json().catch(() => ({}))).detail || r.statusText); e.status = r.status; throw e; }
  return r.json();
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
  if (overlayEl === $('#payOverlay'))   return $('#confirmPay');
  if (overlayEl === $('#shiftOverlay')) return $('#openShift');
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
    if (ov === $('#priceOverlay')) { closePriceCheck(); e.preventDefault(); return; }
    if (ov === $('#payOverlay'))   { closePay(); e.preventDefault(); return; }
    if (ov === $('#ovrOverlay'))   { closeOverride(); e.preventDefault(); return; }
    if (ov === $('#loginOverlay') && S.pinUser) {
      S.pinUser = null; S.pin = ''; renderUsers(); renderPin(); e.preventDefault(); return;
    }
    if (S.checking) { setChecking(false); e.preventDefault(); return; }
    return; // shiftOverlay has no cancel — a shift must be opened to proceed
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
  catch (err) { toast(err.status === 404 ? 'Código no reconocido: ' + code : 'Error al leer', true); return; }
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
  sync();
  return sync;
}
let syncCatsRoving = () => {}, syncGridRoving = () => {};

/* ------------------------------------------------------------------ render */
function renderCats() {
  $('#cats').innerHTML = '';
  S.cats.forEach(c => {
    const b = document.createElement('button');
    b.textContent = c.name; b.className = c.id === S.cat ? 'on' : '';
    // Switching category filters the grid only. The cart is untouched.
    b.onclick = () => { S.cat = c.id; renderCats(); renderGrid(); };
    $('#cats').appendChild(b);
  });
  syncCatsRoving = makeRoving($('#cats'), 'button');
}
function renderGrid() {
  const list = S.cat === 'frecuentes'
    ? S.products.filter(p => p.is_frequent)
    : S.products.filter(p => p.category_id === S.cat);
  $('#grid').innerHTML = '';
  list.forEach(p => {
    const b = document.createElement('button');
    b.className = 'tile';
    b.innerHTML = `<span class="n"></span><span class="p num">${mxn(p.price_cents)}</span>`;
    b.querySelector('.n').textContent = p.name;
    b.onclick = () => S.checking ? (showPrice(p), setChecking(false)) : addToCart(p.id);
    $('#grid').appendChild(b);
  });
  syncGridRoving = makeRoving($('#grid'), '.tile', { grid: true });
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
}
function renderWho() {
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
  const box = $('#userList'); box.innerHTML = '';
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
    b.onclick = () => { S.pinUser = u.id; S.pin = ''; renderUsers(); renderPin(); };
    box.appendChild(b);
  });
  makeRoving($('#userList'), 'button');
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

/* ----------------------------------------------------------------- override */
function askOverride(what, onOk) {
  S.ovr = { what, onOk }; S.ovrPin = '';
  $('#ovrWhat').textContent = what; $('#ovrErr').textContent = '';
  dots($('#ovrDots'), 6, 0, 'var(--amber)');
  openOverlay('#ovrOverlay', k => {
    $('#ovrErr').textContent = '';
    if (k === '←') { S.ovrPin = S.ovrPin.slice(0, -1); } else if (S.ovrPin.length < 6) { S.ovrPin += k; }
    dots($('#ovrDots'), 6, S.ovrPin.length, 'var(--amber)');
    if (S.ovrPin.length === 6) { const p = S.ovrPin; S.ovrPin = ''; S.ovr.onOk(p); }
  });
}
function closeOverride() { closeOverlay('#ovrOverlay'); }

/* ----------------------------------------------------------------- devices
   Polled rather than assumed. The old header claimed the scanner was ready
   whether or not it was plugged in, which trains people to ignore it. */
const DEV_COLOR = { ok: 'var(--green)', blocked: 'var(--amber)', missing: 'var(--red)' };

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
  try { renderDevices(await api('/api/devices')); } catch (e) { /* keep last known */ }
}
setInterval(pollDevices, 5000);

/* --------------------------------------------------------------------- boot */
async function boot() {
  updateInert();
  const b = await api('/api/bootstrap');
  S.users = b.users; S.cats = b.catalogue.categories; S.products = b.catalogue.products;
  S.byId = Object.fromEntries(S.products.map(p => [p.id, p]));
  S.session = b.session; S.shift = b.shift;
  $('#outbox').textContent = b.outbox_pending ? b.outbox_pending + ' por sincronizar' : '';
  renderDevices(b.devices);
  renderUsers(); renderPin(); renderCats(); renderGrid(); renderCart(); renderWho();
  keypad($('#pinPad'), k => S.activeKeypad && S.activeKeypad(k));
  keypad($('#ovrPad'), k => S.activeKeypad && S.activeKeypad(k));
  keypad($('#floatPad'), k => S.activeKeypad && S.activeKeypad(k));
  keypad($('#payPad'), k => S.activeKeypad && S.activeKeypad(k), '.');
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
$('#pcClose').onclick = closePriceCheck;
$('#pcAdd').onclick = () => { addToCart(S.pcProduct.id); closePriceCheck(); };
$('#cobrar').onclick = openPay;
$('#cancelPay').onclick = closePay;
$('#ovrCancel').onclick = closeOverride;

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
    toast(`Ticket #${r.seq} · cambio ${r.change}`);
  } catch (e) { toast('No se pudo cobrar: ' + e.message, true); }
};

$$('#guarded button').forEach(b => b.onclick = () => {
  const act = b.dataset.act;
  if (act === 'cancel') {
    askOverride('Cancelar venta', () => {
      S.cart = []; renderCart(); closeOverride(); toast('Venta cancelada');
    });
  } else if (act === 'drop') {
    toast('Retiro de efectivo — pendiente (Fase 3)');
  }
});

boot().catch(e => {
  // A render error used to leave a blank screen with no clue why. Surface it.
  console.error(e);
  toast('Error al iniciar: ' + e.message, true);
  const d = document.createElement('pre');
  d.style.cssText = 'position:fixed;left:16px;bottom:16px;z-index:99;color:var(--red);'
    + 'font-size:12px;max-width:60vw;white-space:pre-wrap';
  d.textContent = e.stack || String(e);
  document.body.appendChild(d);
});
