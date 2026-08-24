'use strict';

const S = { users: [], cats: [], products: [], byId: {}, cat: 'frecuentes',
            cart: [], session: null, shift: null, checking: false, pcProduct: null,
            pin: '', pinUser: null, ovr: null, ovrPin: '', tendered: '', float: '' };

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

/* ---------------------------------------------------------------- scanning
   The scanner is a keyboard wedge: it types into whatever has focus and presses
   Enter. Capturing at the document level — rather than in a focused text box —
   is deliberate. Their old system has a junk product in it precisely because a
   scan landed in the wrong field. The timeout distinguishes the scanner's burst
   from a person pressing keys. */
let scanBuf = '', scanTimer = null;
document.addEventListener('keydown', e => {
  if (e.key === 'Enter') {
    if (scanBuf.length >= 6) { e.preventDefault(); onScan(scanBuf); }
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
}
function renderCart() {
  const box = $('#lines'); box.innerHTML = '';
  if (!S.cart.length) {
    box.innerHTML = '<div class="empty">Escanea un producto<br>o tócalo en la lista</div>';
  }
  S.cart.forEach(l => {
    const d = document.createElement('div'); d.className = 'line';
    d.innerHTML = `<div class="g"><div class="nm"></div><div class="ea num">${mxn(l.price)} c/u</div></div>
      <div style="display:flex;align-items:center;gap:6px">
        <button class="qbtn" data-d="-1">−</button><span class="num" style="min-width:22px;text-align:center;font-size:15px;font-weight:700">${l.qty}</span>
        <button class="qbtn" data-d="1">+</button></div>
      <span class="tt num">${mxn(l.price * l.qty)}</span>`;
    d.querySelector('.nm').textContent = l.name;
    d.querySelectorAll('.qbtn').forEach(b => b.onclick = () => bump(l.id, +b.dataset.d));
    box.appendChild(d);
  });
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
  $('#priceOverlay').classList.remove('hidden');
}

/* ------------------------------------------------------------------ keypads */
function keypad(el, onKey, extra) {
  el.innerHTML = '';
  ['1','2','3','4','5','6','7','8','9', extra || '', '0', '←'].forEach(k => {
    const b = document.createElement('button');
    b.textContent = k;
    if (k === '') { b.style.visibility = 'hidden'; } else { b.onclick = () => onKey(k); }
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
}
async function pinKey(k) {
  if (!S.pinUser) return;
  $('#pinErr').textContent = '';
  if (k === '←') { S.pin = S.pin.slice(0, -1); renderPin(); return; }
  S.pin += k; renderPin();
  if (S.pin.length < pinLen()) return;
  try {
    const r = await api('/api/login', { method: 'POST', body: JSON.stringify({ user_id: S.pinUser, pin: S.pin }) });
    S.session = r.session; S.pin = '';
    $('#loginOverlay').classList.add('hidden');
    renderWho(); await afterLogin();
  } catch (e) {
    S.pin = ''; renderPin();
    $('#pinErr').textContent = e.message === 'locked' ? 'Bloqueado 5 minutos por intentos fallidos'
                             : e.message === 'bad_pin' ? 'PIN incorrecto' : 'No se pudo entrar';
  }
}

/* -------------------------------------------------------------------- shift */
async function afterLogin() {
  const b = await api('/api/bootstrap');
  S.shift = b.shift; renderWho();
  if (!S.shift) { $('#shiftOverlay').classList.remove('hidden'); renderFloat(); }
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

/* ----------------------------------------------------------------- override */
function askOverride(what, onOk) {
  S.ovr = { what, onOk }; S.ovrPin = '';
  $('#ovrWhat').textContent = what; $('#ovrErr').textContent = '';
  dots($('#ovrDots'), 6, 0, 'var(--amber)');
  $('#ovrOverlay').classList.remove('hidden');
}

/* --------------------------------------------------------------------- boot */
async function boot() {
  const b = await api('/api/bootstrap');
  S.users = b.users; S.cats = b.catalogue.categories; S.products = b.catalogue.products;
  S.byId = Object.fromEntries(S.products.map(p => [p.id, p]));
  S.session = b.session; S.shift = b.shift;
  $('#outbox').textContent = b.outbox_pending ? b.outbox_pending + ' por sincronizar' : '';
  renderUsers(); renderPin(); renderCats(); renderGrid(); renderCart(); renderWho();
  keypad($('#pinPad'), pinKey);
  keypad($('#ovrPad'), k => {
    $('#ovrErr').textContent = '';
    if (k === '←') { S.ovrPin = S.ovrPin.slice(0, -1); } else if (S.ovrPin.length < 6) { S.ovrPin += k; }
    dots($('#ovrDots'), 6, S.ovrPin.length, 'var(--amber)');
    if (S.ovrPin.length === 6) { const p = S.ovrPin; S.ovrPin = ''; S.ovr.onOk(p); }
  });
  keypad($('#floatPad'), k => {
    if (k === '←') S.float = S.float.slice(0, -1); else if (S.float.length < 6) S.float += k;
    renderFloat();
  });
  keypad($('#payPad'), k => {
    if (k === '←') S.tendered = S.tendered.slice(0, -1);
    else if (k === '.') { if (!S.tendered.includes('.')) S.tendered += '.'; }
    else S.tendered += k;
    renderPay();
  }, '.');
  [50, 100, 200, 500].forEach(v => {
    const b2 = document.createElement('button');
    b2.textContent = '$' + v; b2.style.cssText = 'height:48px;border-radius:8px;background:#2a3543;font-size:15px;font-weight:600';
    b2.onclick = () => { S.tendered = String(v); renderPay(); };
    $('#quick').appendChild(b2);
  });

  if (S.session) { $('#loginOverlay').classList.add('hidden'); await afterLogin(); }
}

/* ------------------------------------------------------------------ wiring */
$('#checkBtn').onclick = () => setChecking(!S.checking);
$('#pcClose').onclick = () => $('#priceOverlay').classList.add('hidden');
$('#pcAdd').onclick = () => { addToCart(S.pcProduct.id); $('#priceOverlay').classList.add('hidden'); };
$('#cobrar').onclick = () => { S.tendered = ''; renderPay(); $('#payOverlay').classList.remove('hidden'); };
$('#cancelPay').onclick = () => $('#payOverlay').classList.add('hidden');
$('#ovrCancel').onclick = () => $('#ovrOverlay').classList.add('hidden');

$('#openShift').onclick = async () => {
  await api('/api/shift/open', { method: 'POST',
    body: JSON.stringify({ opening_float_cents: parseInt(S.float || '0', 10) * 100 }) });
  $('#shiftOverlay').classList.add('hidden');
  S.shift = (await api('/api/bootstrap')).shift; renderWho();
  toast('Turno abierto');
};

$('#confirmPay').onclick = async () => {
  const tend = Math.round(parseFloat(S.tendered || '0') * 100);
  try {
    const r = await api('/api/sale', { method: 'POST', body: JSON.stringify({
      lines: S.cart.map(l => ({ product_id: l.id, qty: l.qty })), tendered_cents: tend }) });
    S.cart = []; S.tendered = '';
    $('#payOverlay').classList.add('hidden');
    renderCart();
    toast(`Ticket #${r.seq} · cambio ${r.change}`);
  } catch (e) { toast('No se pudo cobrar: ' + e.message, true); }
};

$$('#guarded button').forEach(b => b.onclick = () => {
  const act = b.dataset.act;
  if (act === 'cancel') {
    askOverride('Cancelar venta', () => {
      S.cart = []; renderCart(); $('#ovrOverlay').classList.add('hidden'); toast('Venta cancelada');
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
