/* Caja Central — admin console.
   Vanilla on purpose: this runs on a LAN box with no build step, and a
   framework would add more to maintain than it saves for a dozen screens. */

const $  = s => document.querySelector(s);
const el = (t, c, x) => { const n = document.createElement(t);
  if (c) n.className = c; if (x != null) n.textContent = x; return n; };

async function api(path, opts) {
  const r = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts));
  if (r.status === 401) { showLogin(); throw new Error('no_session'); }
  if (!r.ok) {
    const e = new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    e.status = r.status; throw e;
  }
  return r.json();
}

const mxn = c => (c < 0 ? '−$' : '$') + Math.abs((Number(c) || 0) / 100)
  .toLocaleString('es-MX', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const dt = s => s ? new Date(s).toLocaleString('es-MX',
  { day: '2-digit', month: '2-digit', year: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—';
const dayLbl = s => s ? new Date(s + 'T12:00:00')
  .toLocaleDateString('es-MX', { day: '2-digit', month: 'short' }) : '—';
const iso = d => d.toISOString().slice(0, 10);

let toastT;
function toast(msg, bad) {
  const t = $('#toast'); t.textContent = msg;
  t.className = bad ? 'bad' : ''; clearTimeout(toastT);
  toastT = setTimeout(() => t.classList.add('hidden'), 3000);
}

/* icons — inline so there is no icon font or sprite to ship */
const ICON = {
  dash:'M3 13h8V3H3v10Zm0 8h8v-6H3v6Zm10 0h8V11h-8v10Zm0-18v6h8V3h-8Z',
  sales:'M3 3v18h18M7 15l4-4 3 3 5-6',
  stock:'M20 7 12 3 4 7v10l8 4 8-4V7ZM4 7l8 4 8-4M12 11v10',
  cat:'M4 6h16M4 12h16M4 18h10',
  shifts:'M12 8v5l3 2m6-2a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z',
  labels:'M3 7h13l5 5-5 5H3V7Zm4 5h.01',
  reports:'M14 3v5h5M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9l-6-6ZM8 13h8M8 17h5',
};
const icon = k => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
  stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="${ICON[k]}"/></svg>`;

/* --------------------------------------------------------------- helpers */
function panel(head, rows, empty) {
  const p = el('div', 'panel');
  if (!rows.length) { p.appendChild(el('div', 'empty', empty || 'Sin datos.')); return p; }
  const t = el('table'), th = el('thead'), tr = el('tr');
  head.forEach(h => { const c = el('th', h.num ? 'num' : null, h.label);
    if (h.w) c.style.width = h.w; tr.appendChild(c); });
  th.appendChild(tr); t.appendChild(th);
  const tb = el('tbody'); rows.forEach(r => tb.appendChild(r)); t.appendChild(tb);
  p.appendChild(t); return p;
}
function tr(cells, cls) {
  const r = el('tr', cls);
  cells.forEach(c => {
    const td = el('td', c.num ? 'num' : (c.cls || null));
    if (c.node) td.appendChild(c.node); else td.textContent = c.text;
    if (c.span) td.colSpan = c.span;
    r.appendChild(td);
  });
  return r;
}
function card(k, v, s, accent) {
  const c = el('div', 'card' + (accent ? ' accent' : ''));
  c.append(el('div', 'k', k), el('div', 'v num', v), el('div', 's', s || ''));
  return c;
}
function chart(rows, valKey, lblKey) {
  const wrap = el('div');
  const max = Math.max(1, ...rows.map(r => Number(r[valKey])));
  const c = el('div', 'chart');
  rows.forEach(r => {
    const col = el('div', 'c');
    const b = el('i'); b.style.height = Math.max(2, (Number(r[valKey]) / max) * 100) + '%';
    col.append(el('b', null, mxn(r[valKey])), b);
    c.appendChild(col);
  });
  const x = el('div', 'chartX');
  rows.forEach(r => x.appendChild(el('span', null, dayLbl(r[lblKey]))));
  wrap.append(c, x); return wrap;
}
function sheet(node) {
  const ov = el('div', 'ov');
  const s = el('div', 'sheet'); s.appendChild(node);
  ov.appendChild(s);
  ov.onclick = e => { if (e.target === ov) ov.remove(); };
  document.addEventListener('keydown', function esc(e) {
    if (e.key === 'Escape') { ov.remove(); document.removeEventListener('keydown', esc); }
  });
  $('#overlay').appendChild(ov);
  const first = s.querySelector('input, select, button');
  if (first) first.focus();
  return ov;
}

/* ------------------------------------------------------------------ state */
const S = { view: 'dash', cats: [], lowCount: 0, labels: [] };

const VIEWS = [
  { id: 'dash',    label: 'Resumen',    icon: 'dash' },
  { id: 'sales',   label: 'Ventas',     icon: 'sales' },
  { id: 'stock',   label: 'Inventario', icon: 'stock' },
  { id: 'cat',     label: 'Catálogo',   icon: 'cat' },
  { id: 'shifts',  label: 'Turnos',     icon: 'shifts' },
  { id: 'labels',  label: 'Etiquetas',  icon: 'labels' },
  { id: 'reports', label: 'Reportes',   icon: 'reports' },
];

function renderNav() {
  const n = $('#nav'); n.innerHTML = '';
  VIEWS.forEach(v => {
    const b = el('button', v.id === S.view ? 'on' : null);
    b.innerHTML = icon(v.icon) + '<span>' + v.label + '</span>';
    if (v.id === 'stock' && S.lowCount) b.appendChild(el('span', 'badge', String(S.lowCount)));
    b.onclick = () => go(v.id);
    n.appendChild(b);
  });
}

function go(id) {
  S.view = id;
  location.hash = id;
  renderNav();
  const v = VIEWS.find(x => x.id === id) || VIEWS[0];
  $('#title').textContent = v.label;
  $('#subtitle').textContent = '';
  $('#headActions').innerHTML = '';
  $('#body').innerHTML = '<div class="empty">Cargando…</div>';
  ({ dash: viewDash, sales: viewSales, stock: viewStock, cat: viewCat,
     shifts: viewShifts, labels: viewLabels, reports: viewReports }[id] || viewDash)()
    .catch(e => { if (e.message !== 'no_session') $('#body').innerHTML =
      '<div class="empty">No se pudo cargar: ' + e.message + '</div>'; });
}

/* ------------------------------------------------------------------- dash */
async function viewDash() {
  const [s, d, st] = await Promise.all([
    api('/api/report/summary'), api('/api/report/by_day?days=21'), api('/api/stock?low_only=true')]);
  const t = s.totals;
  const b = $('#body'); b.innerHTML = '';

  const cards = el('div', 'cards');
  cards.append(
    card('Hoy', mxn(t.cents_today), `${t.tickets_today} ticket${t.tickets_today == 1 ? '' : 's'}`, true),
    card('Últimos 7 días', mxn(t.cents_7d), `${t.tickets_7d} tickets`),
    card('Histórico', mxn(t.cents_all), `${t.tickets_all} tickets`),
    card('Ticket promedio', mxn(t.tickets_all ? Math.round(t.cents_all / t.tickets_all) : 0), 'histórico'));
  b.appendChild(cards);

  if (st.stock.length) {
    const h = el('h2', 'sec'); h.append(document.createTextNode('Requiere atención'),
      el('span', 'pill out', st.stock.length + ' productos'));
    b.appendChild(h);
    b.appendChild(panel(
      [{ label: 'Producto' }, { label: 'Estado' }, { label: 'Existencias', num: true },
       { label: 'Mínimo', num: true }],
      st.stock.slice(0, 8).map(r => tr([
        { text: r.name },
        { node: el('span', 'pill ' + r.state, r.state === 'out' ? 'Agotado' : 'Bajo') },
        { text: r.on_hand, num: true },
        { text: r.reorder_level, num: true }])),
      'Nada bajo mínimo.'));
  }

  b.appendChild(el('h2', 'sec', 'Ventas por día'));
  const p = el('div', 'panel'); p.style.padding = '18px 20px 14px';
  p.appendChild(d.days.length ? chart(d.days.slice().reverse(), 'cents', 'day')
                              : el('div', 'empty', 'Aún no hay ventas.'));
  b.appendChild(p);

  b.appendChild(el('h2', 'sec', 'Cajas'));
  b.appendChild(panel(
    [{ label: 'Caja' }, { label: 'Ventas', num: true }, { label: 'Última sincronización' }],
    s.registers.map(r => tr([{ text: r.name || r.id }, { text: r.sales, num: true },
                             { text: dt(r.last_sync) }])), 'Ninguna caja ha sincronizado.'));
  setSync(s.registers);
}

/* ------------------------------------------------------------------ sales */
async function viewSales() {
  const { sales } = await api('/api/report/sales?limit=150');
  const b = $('#body'); b.innerHTML = '';
  $('#subtitle').textContent = 'Toca una venta para ver sus renglones';
  const rows = [];
  sales.forEach(s => {
    const r = tr([
      { text: '#' + s.seq },
      { text: dt(s.sold_at) },
      { node: el('span', 'pill ' + (s.kind === 'refund' ? 'low' : 'info'),
                s.kind === 'refund' ? 'Devolución' : 'Venta') },
      { text: (s.lines || []).length, num: true },
      { text: mxn(s.total_cents), num: true }], 'click');
    r.tabIndex = 0;
    const det = el('tr', 'hidden'); const td = el('td'); td.colSpan = 5;
    td.style.background = 'var(--bg)';
    (s.lines || []).forEach(l => {
      const row = el('div', 'row'); row.style.padding = '3px 0';
      row.append(el('span', null, l.name_at_sale), el('span', 'spacer'),
        el('span', 'muted num', l.qty + ' × ' + mxn(l.unit_price_cents)),
        el('span', 'num', mxn(l.line_total_cents)));
      row.style.fontSize = '13px'; td.appendChild(row);
    });
    if (!(s.lines || []).length) td.appendChild(el('div', 'faint', 'Sin renglones.'));
    det.appendChild(td);
    const t = () => det.classList.toggle('hidden');
    r.onclick = t;
    r.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); t(); } });
    rows.push(r, det);
  });
  b.appendChild(panel([{ label: 'Ticket' }, { label: 'Fecha' }, { label: 'Tipo' },
    { label: 'Renglones', num: true }, { label: 'Total', num: true }], rows, 'Aún no hay ventas.'));
}

/* -------------------------------------------------------------- inventory */
async function viewStock() {
  const { stock } = await api('/api/stock');
  S.lowCount = stock.filter(r => r.state === 'low' || r.state === 'out').length;
  renderNav();
  const b = $('#body'); b.innerHTML = '';

  const add = el('button', 'btn primary', '+ Registrar entrada');
  add.onclick = () => receivingSheet();
  $('#headActions').appendChild(add);

  const bar = el('div', 'toolbar');
  const search = el('div', 'search');
  search.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>';
  const inp = el('input'); inp.type = 'text'; inp.placeholder = 'Buscar producto…';
  inp.style.width = '260px'; search.appendChild(inp);
  const only = el('button', 'btn', 'Solo bajos');
  bar.append(search, only, el('span', 'spacer'));
  const count = el('span', 'muted'); bar.appendChild(count);
  b.appendChild(bar);

  const host = el('div'); b.appendChild(host);
  let lowOnly = false;
  const draw = () => {
    const q = inp.value.trim().toLowerCase();
    const rows = stock.filter(r => (!lowOnly || r.state === 'low' || r.state === 'out')
                                && (!q || r.name.toLowerCase().includes(q)));
    count.textContent = rows.length + ' de ' + stock.length;
    const LBL = { ok: 'En existencia', low: 'Bajo', out: 'Agotado', untracked: 'Sin seguimiento' };
    host.replaceChildren(panel(
      [{ label: 'Producto' }, { label: 'Categoría' }, { label: 'Estado' },
       { label: 'Existencias', num: true }, { label: 'Mínimo', num: true }, { label: '', w: '90px' }],
      rows.map(r => {
        const btn = el('button', 'btn ghost', 'Ajustar');
        btn.onclick = ev => { ev.stopPropagation(); receivingSheet(r); };
        return tr([
          { text: r.name },
          { text: r.category_name || '—', cls: 'muted' },
          { node: el('span', 'pill ' + (r.state === 'untracked' ? 'na' : r.state), LBL[r.state]) },
          { text: r.state === 'untracked' ? '—' : r.on_hand, num: true },
          { text: r.reorder_level == null ? '—' : r.reorder_level, num: true },
          { node: btn }]);
      }), 'Ningún producto coincide.'));
  };
  inp.oninput = draw;
  only.onclick = () => { lowOnly = !lowOnly; only.classList.toggle('primary', lowOnly); draw(); };
  draw();
}

function receivingSheet(product) {
  const box = el('div');
  box.appendChild(el('h3', null, product ? 'Ajustar: ' + product.name : 'Registrar entrada'));
  box.appendChild(el('div', 'hint',
    'Las existencias se calculan como recibido − vendido. Para corregir una merma, '
    + 'una rotura o un conteo equivocado, registra una cantidad negativa: nunca se '
    + 'edita el historial, se compensa.'));

  const g = el('div', 'grid2');
  const lp = el('label', 'f'); lp.append('Producto');
  const sel = el('select'); sel.style.width = '100%';
  lp.appendChild(sel);
  const lq = el('label', 'f'); lq.append('Cantidad (+ entra, − sale)');
  const qty = el('input'); qty.type = 'number'; qty.value = '1'; lq.appendChild(qty);
  g.append(lp, lq);

  const g2 = el('div', 'grid2'); g2.style.marginTop = '14px';
  const lc = el('label', 'f'); lc.append('Costo unitario (opcional)');
  const cost = el('input'); cost.type = 'text'; cost.placeholder = '0.00'; lc.appendChild(cost);
  const ln = el('label', 'f'); ln.append('Nota (opcional)');
  const note = el('input'); note.type = 'text'; note.placeholder = 'Factura, proveedor, motivo…';
  ln.appendChild(note);
  g2.append(lc, ln);

  const err = el('div', 'err');
  const acts = el('div', 'actions');
  const save = el('button', 'btn primary', 'Guardar');
  const cancel = el('button', 'btn ghost', 'Cancelar');
  acts.append(save, cancel);
  box.append(g, g2, err, acts);
  const ov = sheet(box);
  cancel.onclick = () => ov.remove();

  api('/api/catalogue').then(({ products }) => {
    products.forEach(p => {
      const o = el('option', null, p.name); o.value = p.id;
      if (product && p.id === product.id) o.selected = true;
      sel.appendChild(o);
    });
    if (!product) sel.focus(); else qty.focus();
  });

  save.onclick = async () => {
    const n = parseInt(qty.value, 10);
    if (!n) { err.textContent = 'La cantidad no puede ser cero.'; return; }
    const c = cost.value.trim() ? Math.round(parseFloat(cost.value) * 100) : null;
    try {
      await api('/api/receiving', { method: 'POST', body: JSON.stringify({
        product_id: parseInt(sel.value, 10), qty: n, unit_cost_cents: c,
        note: note.value.trim() || null }) });
      ov.remove(); toast('Movimiento registrado'); go('stock');
    } catch (e) { err.textContent = e.message || 'No se pudo guardar.'; }
  };
}

/* -------------------------------------------------------------- catalogue */
async function viewCat() {
  const [{ categories }, { products }] = await Promise.all([
    api('/api/categories'), api('/api/catalogue?inactive=true')]);
  S.cats = categories;
  const b = $('#body'); b.innerHTML = '';

  const add = el('button', 'btn primary', '+ Nuevo producto');
  add.onclick = () => productSheet(null);
  $('#headActions').appendChild(add);
  $('#subtitle').textContent = products.length + ' productos · ' + categories.length + ' categorías';

  const bar = el('div', 'toolbar');
  const search = el('div', 'search');
  search.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>';
  const inp = el('input'); inp.type = 'text'; inp.placeholder = 'Buscar nombre o código…';
  inp.style.width = '280px'; search.appendChild(inp);
  const cat = el('select');
  // An <option> with no value attribute reports its TEXT as .value, so this
  // "all" entry was matching against every category_id and filtering
  // everything out. The empty string has to be explicit.
  const optAll = el('option', null, 'Todas las categorías'); optAll.value = '';
  cat.appendChild(optAll);
  categories.forEach(c => { const o = el('option', null, c.name); o.value = c.id; cat.appendChild(o); });
  bar.append(search, cat, el('span', 'spacer'));
  const count = el('span', 'muted'); bar.appendChild(count);
  b.appendChild(bar);

  const note = el('div', 'card');
  note.style.cssText = 'margin-bottom:16px;border-color:var(--line2);background:var(--surface2)';
  note.innerHTML = '<div class="k" style="color:var(--amber)">Nota</div>'
    + '<div class="s" style="margin-top:6px">Los cambios hechos aquí <b>todavía no bajan a la caja</b>. '
    + 'El envío del catálogo al registro aún no existe, así que la caja sigue siendo la fuente '
    + 'de verdad para vender. Esto sirve para planear, costear y reportar.</div>';
  b.appendChild(note);

  const host = el('div'); b.appendChild(host);
  const draw = () => {
    const q = inp.value.trim().toLowerCase();
    const rows = products.filter(p =>
      (!cat.value || p.category_id === cat.value) &&
      (!q || p.name.toLowerCase().includes(q) || (p.barcodes || []).some(c => c.includes(q))));
    count.textContent = rows.length + ' de ' + products.length;
    host.replaceChildren(panel(
      [{ label: 'Producto' }, { label: 'Categoría' }, { label: 'Precio', num: true },
       { label: 'Costo', num: true }, { label: 'Margen', num: true }, { label: 'Códigos' },
       { label: 'Estado' }],
      rows.map(p => {
        const margin = (p.cost_cents != null && p.price_cents > 0)
          ? Math.round((1 - p.cost_cents / p.price_cents) * 100) + '%' : '—';
        const nm = el('div'); nm.append(el('div', null, p.name));
        const r = tr([
          { node: nm },
          { text: p.category_name || '—', cls: 'muted' },
          { text: mxn(p.price_cents), num: true },
          { text: p.cost_cents == null ? '—' : mxn(p.cost_cents), num: true },
          { text: margin, num: true, cls: 'muted' },
          { node: el('span', 'sub', (p.barcodes || []).join(', ') || 'sin código') },
          { node: el('span', 'pill ' + (p.is_active ? 'ok' : 'na'), p.is_active ? 'Activo' : 'Inactivo') },
        ], 'click');
        r.tabIndex = 0;
        const open = () => productSheet(p);
        r.onclick = open;
        r.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); open(); } });
        return r;
      }), 'Ningún producto coincide.'));
  };
  inp.oninput = draw; cat.onchange = draw; draw();
}

function productSheet(p) {
  const box = el('div');
  box.appendChild(el('h3', null, p ? 'Editar producto' : 'Nuevo producto'));
  box.appendChild(el('div', 'hint', p ? (p.barcodes || []).join(', ') || 'Sin códigos de barras'
                                      : 'Se creará en el catálogo central.'));
  const mk = (lbl, val, type) => {
    const l = el('label', 'f'); l.append(lbl);
    const i = el('input'); i.type = type || 'text'; i.value = val == null ? '' : val;
    l.appendChild(i); return [l, i];
  };
  const [ln, name]  = mk('Nombre', p ? p.name : '');
  const [lp, price] = mk('Precio', p ? (p.price_cents / 100).toFixed(2) : '');
  const [lc, cost]  = mk('Costo', p && p.cost_cents != null ? (p.cost_cents / 100).toFixed(2) : '');
  const [lr, reord] = mk('Mínimo para alerta', p ? (p.reorder_level ?? '') : '', 'number');

  const lcat = el('label', 'f'); lcat.append('Categoría');
  const cat = el('select');
  S.cats.forEach(c => { const o = el('option', null, c.name); o.value = c.id;
    if (p && p.category_id === c.id) o.selected = true; cat.appendChild(o); });
  lcat.appendChild(cat);

  const lact = el('label', 'f'); lact.append('Estado');
  const act = el('select');
  ['Activo', 'Inactivo'].forEach((t, i) => { const o = el('option', null, t);
    o.value = i ? '' : '1'; if (p && !p.is_active && i) o.selected = true; act.appendChild(o); });
  lact.appendChild(act);

  const g1 = el('div'); g1.appendChild(ln); g1.style.marginBottom = '14px';
  const g2 = el('div', 'grid2'); g2.append(lp, lc); g2.style.marginBottom = '14px';
  const g3 = el('div', 'grid2'); g3.append(lcat, lact); g3.style.marginBottom = '14px';
  const g4 = el('div', 'grid2'); g4.append(lr, el('div'));

  const err = el('div', 'err');
  const acts = el('div', 'actions');
  const save = el('button', 'btn primary', 'Guardar');
  const cancel = el('button', 'btn ghost', 'Cancelar');
  acts.append(save, cancel);
  box.append(g1, g2, g3, g4, err, acts);
  const ov = sheet(box);
  cancel.onclick = () => ov.remove();

  save.onclick = async () => {
    const body = {
      name: name.value.trim(),
      category_id: cat.value,
      price_cents: Math.round(parseFloat(price.value || '0') * 100),
      cost_cents: cost.value.trim() ? Math.round(parseFloat(cost.value) * 100) : null,
      is_active: !!act.value,
      reorder_level: reord.value.trim() ? parseInt(reord.value, 10) : null,
    };
    if (!body.name) { err.textContent = 'El nombre no puede estar vacío.'; return; }
    if (!(body.price_cents >= 0)) { err.textContent = 'Precio inválido.'; return; }
    try {
      if (p) await api('/api/catalogue/products/' + p.id, { method: 'PUT', body: JSON.stringify(body) });
      else   await api('/api/catalogue/products', { method: 'POST', body: JSON.stringify(body) });
      ov.remove(); toast(p ? 'Producto actualizado' : 'Producto creado'); go('cat');
    } catch (e) { err.textContent = e.message || 'No se pudo guardar.'; }
  };
}

/* ----------------------------------------------------------------- shifts */
async function viewShifts() {
  const { shifts } = await api('/api/report/shifts?limit=80');
  const b = $('#body'); b.innerHTML = '';
  b.appendChild(panel(
    [{ label: 'Abierto' }, { label: 'Cerrado' }, { label: 'Tickets', num: true },
     { label: 'Ventas', num: true }, { label: 'Retiros', num: true },
     { label: 'Esperado', num: true }, { label: 'Contado', num: true }, { label: 'Diferencia', num: true }],
    shifts.map(s => {
      const open = s.closed_at == null;
      const d = s.difference_cents;
      const pill = open ? el('span', 'pill info', 'Abierto')
        : el('span', 'pill ' + (d === 0 ? 'ok' : d < 0 ? 'out' : 'low'), mxn(d));
      return tr([
        { text: dt(s.opened_at) }, { text: open ? '—' : dt(s.closed_at) },
        { text: s.tickets, num: true }, { text: mxn(s.sales_cents), num: true },
        { text: s.drops_cents ? '−' + mxn(s.drops_cents) : '—', num: true },
        { text: s.expected_cents == null ? '—' : mxn(s.expected_cents), num: true },
        { text: s.counted_cents == null ? '—' : mxn(s.counted_cents), num: true },
        { node: pill, num: true }]);
    }), 'Aún no hay turnos.'));
}

/* --------------------------------------------------------------- etiquetas
   EAN-13 drawn as inline SVG rects, not styled divs: browsers omit background
   colours when printing unless the operator ticks a checkbox, and a label
   whose only job is to be machine-readable must not depend on that. Same
   reasoning, and the same encoder, as the till's own sheet. */

function eanBars(code) {
  const L = ['0001101','0011001','0010011','0111101','0100011','0110001','0101111','0111011','0110111','0001011'];
  const G = ['0100111','0110011','0011011','0100001','0011101','0111001','0000101','0010001','0001001','0010111'];
  const R = ['1110010','1100110','1101100','1000010','1011100','1001110','1010000','1000100','1001000','1110100'];
  const P = ['LLLLLL','LLGLGG','LLGGLG','LLGGGL','LGLLGG','LGGLLG','LGGGLL','LGLGLG','LGLGGL','LGGLGL'];
  const dg = code.split('').map(Number);
  let bits = '101';
  const par = P[dg[0]];
  for (let i = 1; i <= 6; i++) bits += (par[i - 1] === 'L' ? L : G)[dg[i]];
  bits += '01010';
  for (let i = 7; i <= 12; i++) bits += R[dg[i]];
  bits += '101';
  return bits.split('').map((b, i) => ({
    on: b === '1', tall: i < 3 || (i >= 45 && i < 50) || i >= 92 }));
}

function barcodeSvg(code) {
  const bars = eanBars(code);
  const QL = 11, QR = 7, TALL = 34, SHORT = 30;   // quiet zones EAN-13 requires
  const W = QL + bars.length + QR;
  let rects = '', i = 0;
  // Runs are merged: separate one-module rects can leave hairline gaps where
  // the renderer rounds edges at print scale, and a hairline through a bar is
  // exactly what makes a scan fail.
  while (i < bars.length) {
    if (!bars[i].on) { i++; continue; }
    const start = i, tall = bars[i].tall;
    while (i < bars.length && bars[i].on && bars[i].tall === tall) i++;
    rects += `<rect x="${QL + start}" y="0" width="${i - start}" height="${tall ? TALL : SHORT}"/>`;
  }
  return `<svg class="bc" viewBox="0 0 ${W} ${TALL}" preserveAspectRatio="none"`
       + ` shape-rendering="crispEdges" fill="#101418">${rects}</svg>`;
}

async function viewLabels() {
  const d = await api('/api/labels');
  const b = $('#body'); b.innerHTML = '';
  $('#subtitle').textContent = 'Imprime en una hoja normal — la caja sólo tiene impresora de tickets';

  const printBtn = el('button', 'btn primary', 'Imprimir hoja');
  printBtn.onclick = () => window.print();
  $('#headActions').appendChild(printBtn);

  const wrap = el('div'); wrap.style.cssText = 'display:grid;grid-template-columns:1fr 1fr;gap:20px';

  // ---- left: things needing a code, and things that have one --------------
  const left = el('div'); left.className = 'noprint';
  left.appendChild(el('h2', 'sec', 'Sin código de barras'));
  left.appendChild(panel([{ label: 'Producto' }, { label: 'Precio', num: true }, { label: '', w: '96px' }],
    d.missing.map(p => {
      const gen = el('button', 'btn', 'Generar');
      gen.onclick = async () => {
        gen.disabled = true;
        try {
          const r = await api(`/api/catalogue/products/${p.id}/generate_barcode`, { method: 'POST' });
          toast('Código ' + r.code + ' generado'); go('labels');
        } catch (e) { toast('No se pudo generar', true); gen.disabled = false; }
      };
      return tr([{ text: p.name }, { text: mxn(p.price_cents), num: true }, { node: gen }]);
    }), 'Todos los productos activos tienen código.'));

  left.appendChild(el('h2', 'sec', 'Códigos internos'));
  left.appendChild(panel([{ label: 'Producto' }, { label: 'Código' }, { label: '', w: '104px' }],
    d.internal.map(r => {
      const add = el('button', 'btn ghost', 'A la hoja');
      add.onclick = () => { addLabel(r); toast('Agregado: ' + r.name); };
      return tr([{ text: r.name }, { node: el('span', 'sub', r.code) }, { node: add }]);
    }), 'Aún no se ha generado ningún código interno.'));

  // ---- right: the sheet itself -------------------------------------------
  const right = el('div');
  const head = el('h2', 'sec'); head.className = 'sec noprint';
  head.append(document.createTextNode('Hoja de etiquetas'));
  const cnt = el('span', 'muted'); head.appendChild(cnt);
  const clr = el('button', 'btn ghost', 'Vaciar');
  clr.style.marginLeft = 'auto'; head.appendChild(clr);
  right.appendChild(head);
  const sheetHost = el('div', 'sheetGrid'); right.appendChild(sheetHost);

  const drawSheet = () => {
    cnt.textContent = S.labels.length ? S.labels.length + ' etiquetas' : '';
    sheetHost.innerHTML = '';
    if (!S.labels.length) {
      const e = el('div', 'panel noprint'); e.appendChild(el('div', 'empty',
        'Agrega productos a la hoja desde la izquierda.'));
      sheetHost.appendChild(e); return;
    }
    S.labels.forEach(r => {
      const lab = el('div', 'label');
      lab.innerHTML = `<div class="n"></div><div class="p num"></div>`
        + `<div class="bars">${barcodeSvg(r.code)}</div><div class="c num"></div>`;
      lab.querySelector('.n').textContent = r.name;
      lab.querySelector('.p').textContent = mxn(r.price_cents);
      lab.querySelector('.c').textContent = r.code;
      sheetHost.appendChild(lab);
    });
  };
  window.__drawSheet = drawSheet;
  clr.onclick = () => { S.labels = []; drawSheet(); };
  drawSheet();

  wrap.append(left, right); b.appendChild(wrap);
}

function addLabel(r) {
  if (S.labels.some(x => x.code === r.code)) return;
  S.labels.push({ code: r.code, name: r.name, price_cents: r.price_cents });
  if (window.__drawSheet) window.__drawSheet();
}

/* ---------------------------------------------------------------- reports */
async function viewReports() {
  const b = $('#body'); b.innerHTML = '';
  const today = new Date();
  const monthAgo = new Date(today.getTime() - 29 * 86400000);

  const bar = el('div', 'toolbar');
  const l1 = el('label', 'f'); l1.append('Desde');
  const from = el('input'); from.type = 'date'; from.value = iso(monthAgo); l1.appendChild(from);
  const l2 = el('label', 'f'); l2.append('Hasta');
  const to = el('input'); to.type = 'date'; to.value = iso(today); l2.appendChild(to);
  const run = el('button', 'btn primary', 'Generar');
  const quick = el('div', 'row');
  [['Hoy', 0], ['7 días', 6], ['30 días', 29]].forEach(([t, n]) => {
    const q = el('button', 'btn ghost', t);
    q.onclick = () => { from.value = iso(new Date(Date.now() - n * 86400000));
      to.value = iso(new Date()); load(); };
    quick.appendChild(q);
  });
  bar.append(l1, l2, run, quick, el('span', 'spacer'));
  const print = el('button', 'btn', 'Imprimir / PDF');
  print.onclick = () => window.print();
  bar.appendChild(print);
  b.appendChild(bar);

  const host = el('div'); b.appendChild(host);

  async function load() {
    host.innerHTML = '<div class="empty">Generando…</div>';
    const r = await api(`/api/report/range?start=${from.value}&end=${to.value}`);
    host.innerHTML = '';

    const hdr = el('div', 'printOnly');
    hdr.innerHTML = '<h2 style="margin:0 0 2px">Tienda Balneario — reporte de ventas</h2>'
      + '<div style="color:#555;font-size:12px;margin-bottom:14px">'
      + r.start + ' a ' + r.end + ' · generado ' + new Date().toLocaleString('es-MX') + '</div>';
    host.appendChild(hdr);

    const cards = el('div', 'cards');
    cards.append(
      card('Total vendido', mxn(r.totals.cents), r.start + ' → ' + r.end, true),
      card('Tickets', String(r.totals.tickets), 'en el periodo'),
      card('Ticket promedio', mxn(r.totals.avg_cents), 'por venta'),
      card('Días con venta', String(r.by_day.length), 'con actividad'));
    host.appendChild(cards);

    if (r.by_day.length) {
      host.appendChild(el('h2', 'sec', 'Por día'));
      const p = el('div', 'panel'); p.style.padding = '18px 20px 14px';
      p.appendChild(chart(r.by_day, 'cents', 'day')); host.appendChild(p);
    }

    host.appendChild(el('h2', 'sec', 'Por categoría'));
    host.appendChild(panel([{ label: 'Categoría' }, { label: 'Unidades', num: true },
      { label: 'Importe', num: true }],
      r.by_category.map(c => tr([{ text: c.name }, { text: c.qty, num: true },
        { text: mxn(c.cents), num: true }])), 'Sin ventas en el periodo.'));

    host.appendChild(el('h2', 'sec', 'Por producto'));
    host.appendChild(panel([{ label: 'Producto' }, { label: 'Unidades', num: true },
      { label: 'Importe', num: true }],
      r.by_product.map(p => tr([{ text: p.name }, { text: p.qty, num: true },
        { text: mxn(p.cents), num: true }])), 'Sin ventas en el periodo.'));

    if (r.cash.length) {
      host.appendChild(el('h2', 'sec', 'Movimientos de efectivo'));
      const KIND = { drop: 'Retiros', float_in: 'Fondo', payout: 'Pagos' };
      host.appendChild(panel([{ label: 'Tipo' }, { label: 'Movimientos', num: true },
        { label: 'Importe', num: true }],
        r.cash.map(c => tr([{ text: KIND[c.kind] || c.kind }, { text: c.n, num: true },
          { text: mxn(c.cents), num: true }]))));
    }
  }
  run.onclick = load;
  await load();
}

/* -------------------------------------------------------------- sync pill */
function setSync(registers) {
  const last = (registers || []).map(r => r.last_sync).filter(Boolean).sort().pop();
  const mins = last ? (Date.now() - new Date(last)) / 60000 : null;
  $('#syncTxt').textContent = last ? 'Sinc. ' + dt(last) : 'Sin sincronizar';
  $('#syncDot').className = 'dot ' + (mins == null ? 'bad' : mins < 5 ? 'ok' : mins < 60 ? 'warn' : 'bad');
}
async function refreshSync() {
  try { setSync((await api('/api/report/summary')).registers); } catch (e) { /* keep last */ }
}

/* ------------------------------------------------------------------- auth */
function showLogin() {
  $('#app').classList.add('hidden');
  $('#login').classList.remove('hidden');
  setTimeout(() => $('#pw').focus(), 30);
}
function showApp() {
  $('#login').classList.add('hidden');
  $('#app').classList.remove('hidden');
  renderNav();
  go(VIEWS.some(v => v.id === location.hash.slice(1)) ? location.hash.slice(1) : 'dash');
  refreshSync();
}
async function doLogin() {
  const err = $('#loginErr'); err.textContent = '';
  try {
    await api('/api/login', { method: 'POST', body: JSON.stringify({ password: $('#pw').value }) });
    $('#pw').value = '';
    showApp();
  } catch (e) {
    err.textContent = e.message === 'no_password_configured'
      ? 'El servidor no tiene contraseña configurada.' : 'Contraseña incorrecta.';
  }
}
$('#loginBtn').onclick = doLogin;
$('#pw').addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });
$('#logoutBtn').onclick = async () => {
  await fetch('/api/logout', { method: 'POST' }); showLogin();
};
$('#refreshBtn').onclick = () => { go(S.view); refreshSync(); toast('Actualizado'); };
window.addEventListener('hashchange', () => {
  const h = location.hash.slice(1);
  if (h && h !== S.view && VIEWS.some(v => v.id === h)) go(h);
});
setInterval(refreshSync, 45000);

(async () => {
  const s = await fetch('/api/session').then(r => r.json()).catch(() => ({}));
  if (s.authenticated) showApp(); else showLogin();
})();
