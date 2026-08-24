/* Central reporting UI.
   Read-only on purpose: the till owns its catalogue until the catalogue push
   exists, and two masters is worse than one screen that cannot edit. */

const $ = s => document.querySelector(s);
const api = async p => {
  const r = await fetch(p);
  if (!r.ok) throw new Error(r.status + ' ' + r.statusText);
  return r.json();
};
const mxn = c => (c < 0 ? '−$' : '$') +
  Math.abs((c || 0) / 100).toLocaleString('es-MX', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const dt = s => s ? new Date(s).toLocaleString('es-MX',
  { day: '2-digit', month: '2-digit', year: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—';
const day = s => s ? new Date(s + 'T12:00:00').toLocaleDateString('es-MX',
  { weekday: 'short', day: '2-digit', month: 'short' }) : '—';
const el = (tag, cls, txt) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (txt != null) n.textContent = txt;
  return n;
};

function table(head, rows, opts = {}) {
  if (!rows.length) return el('div', 'empty', opts.empty || 'Sin datos todavía.');
  const t = el('table');
  const thead = el('thead'), tr = el('tr');
  head.forEach(h => {
    const th = el('th', h.num ? 'num' : null, h.label);
    tr.appendChild(th);
  });
  thead.appendChild(tr); t.appendChild(thead);
  const tb = el('tbody');
  rows.forEach(r => tb.appendChild(r));
  t.appendChild(tb);
  return t;
}

function row(cells, cls) {
  const tr = el('tr', cls);
  cells.forEach(c => {
    const td = el('td', c.num ? 'num' : (c.cls || null));
    if (c.node) td.appendChild(c.node); else td.textContent = c.text;
    if (c.span) td.colSpan = c.span;
    tr.appendChild(td);
  });
  return tr;
}

/* ------------------------------------------------------------------ views */
async function loadDash() {
  const s = await api('/api/report/summary');
  const t = s.totals;
  const kpi = (k, v, sub) => {
    const c = el('div', 'card');
    c.append(el('div', 'k', k), el('div', 'v num', v), el('div', 's', sub || ''));
    return c;
  };
  $('#kpis').replaceChildren(
    kpi('Hoy', mxn(t.cents_today), `${t.tickets_today} ticket${t.tickets_today === 1 ? '' : 's'}`),
    kpi('Últimos 7 días', mxn(t.cents_7d), `${t.tickets_7d} tickets`),
    kpi('Histórico', mxn(t.cents_all), `${t.tickets_all} tickets`));

  const d = await api('/api/report/by_day?days=30');
  const max = Math.max(1, ...d.days.map(x => Number(x.cents)));
  $('#byDay').replaceChildren(table(
    [{ label: 'Día' }, { label: 'Tickets', num: true }, { label: 'Total', num: true }, { label: '' }],
    d.days.map(x => {
      const bar = el('div', 'bar');
      bar.style.width = Math.round((Number(x.cents) / max) * 100) + '%';
      return row([{ text: day(x.day) }, { text: x.tickets, num: true },
                  { text: mxn(x.cents), num: true }, { node: bar }]);
    }), { empty: 'Aún no hay ventas sincronizadas.' }));

  $('#registers').replaceChildren(table(
    [{ label: 'Caja' }, { label: 'Ventas', num: true }, { label: 'Última sincronización' }],
    s.registers.map(r => row([
      { text: r.name || r.id },
      { text: r.sales, num: true },
      { text: dt(r.last_sync) }]))));

  setSyncPill(s.registers);
}

// The pill lives in the header on every view, so it cannot depend on the
// summary tab being the one on screen.
function setSyncPill(registers) {
  const last = registers.map(r => r.last_sync).filter(Boolean).sort().pop();
  const mins = last ? (Date.now() - new Date(last)) / 60000 : null;
  $('#syncTxt').textContent = last ? 'Sincronizado ' + dt(last) : 'Sin sincronizar';
  $('#syncPill').querySelector('.dot').style.background =
    mins == null ? 'var(--red)' : mins < 10 ? 'var(--green)' : 'var(--amber)';
}

async function refreshSyncPill() {
  try { setSyncPill((await api('/api/report/summary')).registers); } catch (e) { /* keep last */ }
}

async function loadSales() {
  const { sales } = await api('/api/report/sales?limit=100');
  const rows = [];
  sales.forEach(s => {
    const tr = row([
      { text: '#' + s.seq },
      { text: dt(s.sold_at) },
      { text: s.kind === 'refund' ? 'Devolución' : 'Venta', cls: s.kind === 'refund' ? 'warn' : null },
      { text: (s.lines || []).length, num: true },
      { text: mxn(s.total_cents), num: true }], 'clickable');
    tr.tabIndex = 0;
    const det = el('tr', 'lines hidden');
    const td = el('td'); td.colSpan = 5;
    (s.lines || []).forEach(l => {
      const line = el('div', 'l');
      line.append(el('span', 'nm', l.name_at_sale),
                  el('span', 'num', l.qty + ' × ' + mxn(l.unit_price_cents)),
                  el('span', 'num', mxn(l.line_total_cents)));
      td.appendChild(line);
    });
    if (!(s.lines || []).length) td.appendChild(el('div', 'l', 'Sin renglones sincronizados.'));
    det.appendChild(td);
    const toggle = () => det.classList.toggle('hidden');
    tr.onclick = toggle;
    tr.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); toggle(); } });
    rows.push(tr, det);
  });
  $('#salesTable').replaceChildren(table(
    [{ label: 'Ticket' }, { label: 'Fecha' }, { label: 'Tipo' },
     { label: 'Renglones', num: true }, { label: 'Total', num: true }],
    rows, { empty: 'Aún no hay ventas sincronizadas.' }));
}

async function loadShifts() {
  const { shifts } = await api('/api/report/shifts?limit=50');
  $('#shiftsTable').replaceChildren(table(
    [{ label: 'Abierto' }, { label: 'Cerrado' }, { label: 'Tickets', num: true },
     { label: 'Ventas', num: true }, { label: 'Retiros', num: true },
     { label: 'Esperado', num: true }, { label: 'Contado', num: true }, { label: 'Diferencia', num: true }],
    shifts.map(s => {
      const d = s.difference_cents;
      // An open shift has no count yet; showing 0.00 would read as "cuadra".
      const diffTxt = s.closed_at == null ? 'abierto' : mxn(d);
      const cls = s.closed_at == null ? 'muted' : d === 0 ? 'ok' : d < 0 ? 'bad' : 'warn';
      return row([
        { text: dt(s.opened_at) },
        { text: s.closed_at ? dt(s.closed_at) : '—' },
        { text: s.tickets, num: true },
        { text: mxn(s.sales_cents), num: true },
        { text: s.drops_cents ? '−' + mxn(s.drops_cents) : '—', num: true },
        { text: s.expected_cents == null ? '—' : mxn(s.expected_cents), num: true },
        { text: s.counted_cents == null ? '—' : mxn(s.counted_cents), num: true },
        { node: el('span', cls + ' num', diffTxt), num: true }]);
    }), { empty: 'Aún no hay turnos sincronizados.' }));
}

async function loadProducts() {
  const { products } = await api('/api/report/products?limit=100');
  const max = Math.max(1, ...products.map(p => Number(p.qty)));
  $('#productsTable').replaceChildren(table(
    [{ label: 'Producto' }, { label: 'Unidades', num: true }, { label: 'Importe', num: true }, { label: '' }],
    products.map(p => {
      const bar = el('div', 'bar');
      bar.style.width = Math.round((Number(p.qty) / max) * 100) + '%';
      return row([{ text: p.name }, { text: p.qty, num: true },
                  { text: mxn(p.cents), num: true }, { node: bar }]);
    }), { empty: 'Aún no hay ventas sincronizadas.' }));
}

const VIEWS = { dash: loadDash, sales: loadSales, shifts: loadShifts, products: loadProducts };

function show(name) {
  document.querySelectorAll('#nav button').forEach(b => b.classList.toggle('on', b.dataset.view === name));
  ['dash', 'sales', 'shifts', 'products'].forEach(v =>
    $('#view' + v[0].toUpperCase() + v.slice(1)).classList.toggle('hidden', v !== name));
  VIEWS[name]().catch(e => console.error(e));
}

document.querySelectorAll('#nav button').forEach(b => b.onclick = () => { location.hash = b.dataset.view; });
// Hash routing so a view is linkable and survives a refresh -- someone who
// lives on "Turnos y cortes" should not land on the summary every morning.
window.addEventListener('hashchange', () => show(VIEWS[location.hash.slice(1)] ? location.hash.slice(1) : 'dash'));
show(VIEWS[location.hash.slice(1)] ? location.hash.slice(1) : 'dash');
refreshSyncPill();
setInterval(refreshSyncPill, 45000);
// The till drains every 30 s; refreshing the summary a little slower than that
// keeps the header honest without hammering Postgres.
setInterval(() => { if (!$('#viewDash').classList.contains('hidden')) loadDash().catch(() => {}); }, 45000);
