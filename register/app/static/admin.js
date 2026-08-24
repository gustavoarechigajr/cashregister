'use strict';

window.addEventListener('error', e => showFatalError('Error', e.error || e.message));
window.addEventListener('unhandledrejection', e => showFatalError('Error async', e.reason));
function showFatalError(label, err) {
  const d = document.createElement('pre');
  d.style.cssText = 'position:fixed;left:16px;bottom:16px;z-index:999;color:#ff5f5f;'
    + 'background:#000;padding:8px;font-size:12px;max-width:80vw;white-space:pre-wrap';
  d.textContent = label + ': ' + (err && (err.stack || err.message) || String(err));
  document.body.appendChild(d);
}

const S = { session: null, products: [], categories: [], view: 'products',
            q: '', filterMissing: false, editing: null, pending: [] };

const $  = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
const mxn = c => (c < 0 ? '−$' : '$') + Math.abs(c / 100).toLocaleString('es-MX',
             { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const toCents = v => (v === '' || v === null || v === undefined) ? null
                    : Math.round(parseFloat(v) * 100);

async function api(path, opts) {
  const r = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts));
  if (r.status === 403) { window.location.href = '/'; throw new Error('not_admin'); }
  if (!r.ok) { const e = new Error((await r.json().catch(() => ({}))).detail || r.statusText); e.status = r.status; throw e; }
  return r.json();
}

let toastTimer;
function toast(msg, bad) {
  const t = $('#toast'); t.textContent = msg; t.className = bad ? 'bad' : '';
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add('hidden'), 2600);
}

/* --------------------------------------------------------------- EAN-13
   Same encoder as the sell-screen barcode preview: real bars, not a mockup
   -- what prints here is what a scanner will actually read. */
function eanCheckDigit(payload12) {
  let s = 0;
  for (let i = 0; i < 12; i++) s += Number(payload12[i]) * (i % 2 === 0 ? 1 : 3);
  return (10 - (s % 10)) % 10;
}
function eanBars(fullCode13) {
  const L = ['0001101','0011001','0010011','0111101','0100011','0110001','0101111','0111011','0110111','0001011'];
  const G = ['0100111','0110011','0011011','0100001','0011101','0111001','0000101','0010001','0001001','0010111'];
  const R = ['1110010','1100110','1101100','1000010','1011100','1001110','1010000','1000100','1001000','1110100'];
  const P = ['LLLLLL','LLGLGG','LLGGLG','LLGGGL','LGLLGG','LGGLLG','LGGGLL','LGLGLG','LGLGGL','LGGLGL'];
  const dg = fullCode13.split('').map(Number);
  let bits = '101';
  const par = P[dg[0]];
  for (let i = 1; i <= 6; i++) bits += (par[i - 1] === 'L' ? L : G)[dg[i]];
  bits += '01010';
  for (let i = 7; i <= 12; i++) bits += R[dg[i]];
  bits += '101';
  return bits.split('').map((b, i) => {
    const guard = i < 3 || (i >= 45 && i < 50) || i >= 92;
    return { on: b === '1', tall: guard };
  });
}

/* ------------------------------------------------------------------ boot */
async function boot() {
  const s = await api('/api/admin/session');
  S.session = s.session;
  renderWho();
  await loadProducts();
  renderCategoryOptions();
  await loadMissing();
  await loadInternal();
  await loadSettings();
  wireGlobalKeys();
  // Same trap fixed repeatedly on the till: a fresh page sets no focus at
  // all, so the first Tab lands on whatever comes first in markup order --
  // here, "Volver a caja", not the search box. A keyboard user's first
  // keystroke would silently exit the page they just arrived on.
  $('#q').focus();
}
function renderWho() {
  if (!S.session) return;
  const ini = S.session.name.split(' ').map(w => w[0]).slice(0, 2).join('');
  $('#who').innerHTML = `<div class="avatar">${ini}</div><div style="line-height:1.15">
    <div style="font-size:13px;font-weight:600">${S.session.name}</div>
    <div class="muted" style="font-size:11px">Administrador</div></div>`;
}

/* -------------------------------------------------------------------- nav */
function switchView(view) {
  S.view = view;
  $$('#admNav button').forEach(b => b.classList.toggle('on', b.dataset.view === view));
  $$('.view').forEach(v => v.classList.add('hidden'));
  $(`#view${view[0].toUpperCase()}${view.slice(1)}`).classList.remove('hidden');
}

/* -------------------------------------------------------------- products */
async function loadProducts() {
  const params = new URLSearchParams();
  if (S.q) params.set('q', S.q);
  if (S.filterMissing) params.set('missing_barcode', 'true');
  const r = await api('/api/admin/products?' + params.toString());
  S.products = r.products; S.categories = r.categories;
  renderProductTable();
}
function renderCategoryOptions() {
  $('#fCategory').innerHTML = S.categories.map(c =>
    `<option value="${c.id}">${c.name}</option>`).join('');
}
/* ------------------------------------------------ focus across re-renders
   Same helper as app.js (the two files share no module, so this is a
   deliberate copy -- keep them in step). Rebuilding a list destroys the node
   the keyboard user was standing on; without this, generating a run of
   barcodes or saving an edit drops focus to <body> and forces them back to
   the mouse every time. Restores by key where rows carry one, else by
   position, so a row that disappears hands focus to its neighbour. */
function keepFocus(box, rebuild, keyOf) {
  const active = document.activeElement;
  const kids = () => Array.from(box.children);
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
  if (box.dataset.roving) {
    list.forEach(el => { if (el !== target) el.tabIndex = -1; });
    target.tabIndex = 0;
    target.focus();
    return;
  }
  const focusable = target.tabIndex >= 0 ? target : target.querySelector('[tabindex], button');
  if (focusable) focusable.focus();
}

function renderProductTable() {
  const box = $('#productRows');
  keepFocus(box, () => {
    box.innerHTML = '';
    S.products.forEach(p => {
      const tr = document.createElement('tr');
      tr.tabIndex = 0; tr.dataset.pid = p.id;
      const margin = (p.cost_cents != null && p.price_cents > 0)
        ? Math.round((1 - p.cost_cents / p.price_cents) * 100) + '%' : '—';
      const codes = p.barcodes.length
        ? p.barcodes.map(b => b.code).join(', ')
        : 'sin código';
      tr.innerHTML = `
        <td class="rowName"></td>
        <td class="muted"></td>
        <td class="num">${mxn(p.price_cents)}</td>
        <td class="num muted">${p.cost != null ? p.cost : '—'}</td>
        <td class="num" style="color:var(--blue)">${margin}</td>
        <td><span class="rowCode num${p.barcodes.length ? '' : ' missing'}"></span></td>
        <td><span class="badge ${p.is_active ? 'on' : 'off'}">${p.is_active ? 'Activo' : 'Inactivo'}</span></td>`;
      tr.querySelector('.rowName').textContent = p.name;
      tr.children[1].textContent = p.category_name;
      tr.querySelector('.rowCode').textContent = codes;
      const open = () => openEdit(p);
      tr.onclick = open;
      tr.addEventListener('keydown', e => { if (e.key === 'Enter') open(); });
      box.appendChild(tr);
    });
  }, el => el.dataset.pid);
  $('#shownCount').textContent = S.products.length === 1 ? '1 producto' : S.products.length + ' productos';
}

/* --------------------------------------------------------------- editing */
function openEdit(product) {
  S.editing = product ? { ...product } : null;
  // Codes typed for a product that does not exist yet. Previously the barcode
  // controls simply refused to work until you saved, hunted the product back
  // down in the table and reopened it -- three steps to do one thing. They are
  // held here and applied immediately after the product is created.
  S.pendingCodes = [];
  S.pendingGen = false;
  $('#editTitle').textContent = product ? 'Editar producto' : 'Nuevo producto';
  $('#fName').value = product ? product.name : '';
  $('#fCategory').value = product ? product.category_id : (S.categories[0] && S.categories[0].id);
  $('#fActive').checked = product ? !!product.is_active : true;
  $('#fPrice').value = product ? (product.price_cents / 100).toFixed(2) : '';
  $('#fCost').value = product && product.cost_cents != null ? (product.cost_cents / 100).toFixed(2) : '';
  $('#fNewCode').value = '';
  $('#editErr').textContent = '';
  renderEditBarcodes();
  // Only for products that exist. There is nothing to delete on a new one, and
  // showing a dead Eliminar next to Guardar invites a misclick.
  $('#editDelete').classList.toggle('hidden', !(product && product.id));
  $('#editOverlay').classList.remove('hidden');
  $('#adm').setAttribute('inert', '');
  $('#fName').focus();
}
function closeEdit() {
  $('#editOverlay').classList.add('hidden');
  $('#adm').removeAttribute('inert');
  S.editing = null;
}
function renderEditBarcodes() {
  const box = $('#editBarcodes');
  const codes = (S.editing && S.editing.barcodes) || [];
  const pending = S.pendingCodes || [];
  keepFocus(box, () => {
    box.innerHTML = '';
    if (!codes.length && !pending.length && !S.pendingGen) {
      box.innerHTML = '<div class="muted" style="font-size:13px">Sin códigos asignados.</div>';
    }
    // Pending entries are visibly distinct: they do not exist server-side yet
    // and are only written when the product is saved.
    pending.forEach(code => {
      const row = document.createElement('div'); row.className = 'barcodeRow';
      row.innerHTML = `<span class="code"></span>
        <span class="tag" style="border-color:#4a3a28;color:var(--amber)">al guardar</span>
        <button aria-label="Quitar">✕</button>`;
      row.querySelector('.code').textContent = code;
      row.querySelector('button').onclick = () => {
        S.pendingCodes = S.pendingCodes.filter(c => c !== code);
        renderEditBarcodes();
      };
      box.appendChild(row);
    });
    if (S.pendingGen) {
      const row = document.createElement('div'); row.className = 'barcodeRow';
      row.innerHTML = `<span class="code muted">se generará automáticamente</span>
        <span class="tag" style="border-color:#4a3a28;color:var(--amber)">al guardar</span>
        <button aria-label="Quitar">✕</button>`;
      row.querySelector('button').onclick = () => { S.pendingGen = false; renderEditBarcodes(); };
      box.appendChild(row);
    }
    codes.forEach(b => {
      const row = document.createElement('div'); row.className = 'barcodeRow';
      row.dataset.code = b.code;
      row.innerHTML = `<span class="code"></span>
        ${b.is_internal ? '<span class="tag">interno</span>' : ''}
        <button aria-label="Eliminar">✕</button>`;
      row.querySelector('.code').textContent = b.code;
      row.querySelector('button').onclick = async () => {
        await api('/api/admin/barcodes/' + encodeURIComponent(b.code), { method: 'DELETE' });
        S.editing.barcodes = S.editing.barcodes.filter(x => x.code !== b.code);
        renderEditBarcodes();
        await loadProducts(); await loadMissing(); await loadInternal();
      };
      box.appendChild(row);
    });
  }, el => el.dataset.code);
}
async function saveEdit() {
  const name = $('#fName').value.trim();
  if (!name) { $('#editErr').textContent = 'El nombre no puede estar vacío.'; $('#fName').focus(); return; }
  const price = toCents($('#fPrice').value);
  if (price === null || price < 0) { $('#editErr').textContent = 'Precio inválido.'; $('#fPrice').focus(); return; }
  const cost = toCents($('#fCost').value);
  const category_id = $('#fCategory').value;
  const is_active = $('#fActive').checked;

  try {
    if (S.editing && S.editing.id) {
      await api(`/api/admin/products/${S.editing.id}`, { method: 'PUT', body: JSON.stringify({
        name, category_id, price_cents: price, is_active,
        cost_cents: cost, cost_cents_set: true }) });
      toast('Producto actualizado');
    } else {
      const p = await api('/api/admin/products', { method: 'POST', body: JSON.stringify({
        name, category_id, price_cents: price, cost_cents: cost, is_active }) });
      S.editing = p;
      // Apply whatever was queued while the product did not exist yet. A code
      // that turns out to be taken must not silently vanish -- the product is
      // already saved at this point, so report it and leave the panel open
      // rather than closing over a half-applied change.
      const failed = [];
      for (const code of S.pendingCodes) {
        try {
          const upd = await api(`/api/admin/products/${p.id}/barcode`, {
            method: 'POST', body: JSON.stringify({ code }) });
          S.editing.barcodes = upd.barcodes;
        } catch (err) {
          failed.push(code + (err.message === 'barcode_in_use' ? ' (ya está en uso)' : ''));
        }
      }
      if (S.pendingGen) {
        try {
          const upd = await api(`/api/admin/products/${p.id}/generate_barcode`, { method: 'POST' });
          S.editing.barcodes = upd.barcodes;
        } catch (err) { failed.push('código automático'); }
      }
      S.pendingCodes = []; S.pendingGen = false;
      if (failed.length) {
        await loadProducts(); await loadMissing(); await loadInternal();
        renderEditBarcodes();
        $('#editErr').textContent = 'Producto creado, pero no se pudo agregar: ' + failed.join(', ');
        toast('Producto creado con avisos', true);
        return;
      }
      toast('Producto creado');
    }
    await loadProducts(); await loadMissing(); await loadInternal();
    closeEdit();
  } catch (e) {
    $('#editErr').textContent = e.message || 'No se pudo guardar.';
  }
}

/* ---------------------------------------------------------------- barcodes (screen) */
async function loadMissing() {
  const r = await api('/api/admin/products?missing_barcode=true');
  renderMissing(r.products.filter(p => p.is_active));
}

async function loadInternal() {
  // Every product that already carries an internal code, listed permanently.
  // The panel used to show only products *missing* a code, so the moment you
  // generated one the product dropped off the screen and there was no way to
  // print its label again -- exactly when you need it (label peeled off, new
  // shelf tag, reprinting a damaged sheet).
  const r = await api('/api/admin/products');
  const rows = [];
  (r.products || []).forEach(p => (p.barcodes || []).forEach(b => {
    if (b.is_internal) rows.push({ id: p.id, name: p.name, price_cents: p.price_cents, code: b.code });
  }));
  rows.sort((a, b) => a.name.localeCompare(b.name, 'es'));
  renderInternal(rows);
}

function renderInternal(rows) {
  const box = $('#internalList');
  $('#internalCount').textContent = rows.length === 1 ? '1 código' : rows.length + ' códigos';
  keepFocus(box, () => {
    box.innerHTML = '';
    if (!rows.length) {
      box.innerHTML = '<div class="muted" style="padding:14px">Aún no se ha generado ningún código interno.</div>';
      return;
    }
    rows.forEach(r => {
      const row = document.createElement('div'); row.className = 'pendingRow';
      row.dataset.code = r.code;
      row.innerHTML = `<div class="info"><div class="rowName"></div>
        <div class="price num"></div></div>
        <button class="small">Agregar a hoja</button>`;
      row.querySelector('.rowName').textContent = r.name;
      row.querySelector('.price').textContent = r.code + ' · ' + mxn(r.price_cents);
      const btn = row.querySelector('button');
      const already = S.pending.some(x => x.code === r.code);
      if (already) { btn.textContent = 'En la hoja'; btn.disabled = true; }
      btn.onclick = () => {
        if (S.pending.some(x => x.code === r.code)) return;
        S.pending.push({ id: r.id, name: r.name, price_cents: r.price_cents, code: r.code });
        renderSheet();
        renderInternal(rows);
        toast(`Agregado: ${r.name}`);
      };
      box.appendChild(row);
    });
  }, el => el.dataset.code);
}
function renderMissing(list) {
  const box = $('#missingList');
  keepFocus(box, () => {
    box.innerHTML = '';
    if (!list.length) {
      box.innerHTML = '<div class="muted" style="padding:14px">Todos los productos activos tienen código.</div>';
    }
    list.forEach(p => {
      const row = document.createElement('div'); row.className = 'pendingRow';
      row.dataset.pid = String(p.id);
      row.innerHTML = `<div class="info"><div class="rowName"></div>
        <div class="price num"></div></div>
        <button class="small primary">Generar</button>`;
      row.querySelector('.rowName').textContent = p.name;
      row.querySelector('.price').textContent = mxn(p.price_cents);
      row.querySelector('button').onclick = async () => {
        const updated = await api(`/api/admin/products/${p.id}/generate_barcode`, { method: 'POST' });
        const code = updated.barcodes[updated.barcodes.length - 1].code;
        S.pending.push({ id: p.id, name: p.name, price_cents: p.price_cents, code });
        renderSheet();
        await loadMissing(); await loadProducts(); await loadInternal();
        toast(`Código generado para ${p.name}`);
      };
      box.appendChild(row);
    });
  }, el => el.dataset.pid);
}
function barcodeSvg(code) {
  /* Inline SVG, not styled divs.
     The old version drew each module as a <div> whose only visual was a
     background colour, and Chromium omits background graphics when printing
     unless the operator ticks "Background graphics" in the dialog. The labels
     came out with the name, price and digits but a blank space where the
     barcode belongs -- which looks fine until someone tries to scan it. SVG
     rects are page content, so they print regardless of that checkbox.

     Runs of adjacent modules are merged into one rect: at print scale,
     separate 1-module rects can leave hairline gaps where the renderer rounds
     edges, and a hairline through a bar is exactly what makes a scan fail. */
  const bars = eanBars(code);
  const QUIET_L = 11, QUIET_R = 7;          // EAN-13 requires quiet zones
  const W = QUIET_L + bars.length + QUIET_R;
  const TALL = 34, SHORT = 30;

  let rects = '', i = 0;
  while (i < bars.length) {
    if (!bars[i].on) { i++; continue; }
    const start = i, tall = bars[i].tall;
    while (i < bars.length && bars[i].on && bars[i].tall === tall) i++;
    rects += `<rect x="${QUIET_L + start}" y="0" width="${i - start}" height="${tall ? TALL : SHORT}"/>`;
  }
  return `<svg class="bc" viewBox="0 0 ${W} ${TALL}" preserveAspectRatio="none"`
       + ` shape-rendering="crispEdges" fill="#101418" role="img"`
       + ` aria-label="Código ${code}">${rects}</svg>`;
}

function renderSheet() {
  const grid = $('#sheetGrid'); grid.innerHTML = '';
  S.pending.forEach(item => {
    const label = document.createElement('div'); label.className = 'label';
    label.innerHTML = `<div class="n"></div><div class="p num"></div>
      <div class="bars">${barcodeSvg(item.code)}</div><div class="c num"></div>`;
    label.querySelector('.n').textContent = item.name;
    label.querySelector('.p').textContent = mxn(item.price_cents);
    label.querySelector('.c').textContent = item.code;
    grid.appendChild(label);
  });
  $('#sheetCount').textContent = S.pending.length === 1 ? '1 etiqueta' : S.pending.length + ' etiquetas';
  $('#printSheet').disabled = !S.pending.length;
}

/* -------------------------------------------------------------- settings */
async function loadSettings() {
  try {
    const r = await api('/api/admin/settings');
    renderSettings(r.test_mode);
  } catch (e) { toast('No se pudieron leer los ajustes', true); }
}
function renderSettings(on) {
  S.testMode = !!on;
  $('#testToggle').setAttribute('aria-checked', S.testMode ? 'true' : 'false');
  const note = $('#testState');
  note.textContent = S.testMode
    ? 'Activo: las ventas se guardan, pero no se imprime ni se abre el cajón.'
    : 'Apagado: la caja imprime el ticket y abre el cajón en cada venta.';
  note.classList.toggle('on', S.testMode);
}
async function toggleTestMode() {
  const next = !S.testMode;
  try {
    const r = await api('/api/admin/settings',
      { method: 'PUT', body: JSON.stringify({ test_mode: next }) });
    renderSettings(r.test_mode);
    toast(r.test_mode ? 'Modo de pruebas ACTIVADO' : 'Modo de pruebas apagado');
  } catch (e) { toast('No se pudo cambiar el ajuste', true); }
}

/* ------------------------------------------------------------------ keys */
function wireGlobalKeys() {
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    // Escape unwinds one level at a time: the edit panel first, then a live
    // search filter, then out of the admin panel entirely. The last step
    // matters -- "Volver a caja" is the first control in the DOM, so from a
    // product row the only keyboard route back was Shift+Tab through every
    // row on screen. That is not navigation, and a cashier will just grab
    // the mouse.
    if (!$('#editOverlay').classList.contains('hidden')) { closeEdit(); e.preventDefault(); return; }
    if ($('#q').value) {
      $('#q').value = ''; S.q = ''; loadProducts();
      e.preventDefault(); return;
    }
    e.preventDefault();
    window.location.href = '/';
  });
  $('#fNewCode').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); addManualCode(); } });
  $('#q').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); S.q = $('#q').value; loadProducts(); } });
}
async function addManualCode() {
  const code = $('#fNewCode').value.trim();
  if (!code) return;
  if (!S.editing || !S.editing.id) {
    // New product: queue it. Validation against other products still happens
    // server-side on save, which is the only place it can be authoritative.
    if (!S.pendingCodes.includes(code)) S.pendingCodes.push(code);
    $('#fNewCode').value = '';
    $('#editErr').textContent = '';
    renderEditBarcodes();
    return;
  }
  try {
    const updated = await api(`/api/admin/products/${S.editing.id}/barcode`, {
      method: 'POST', body: JSON.stringify({ code }) });
    S.editing.barcodes = updated.barcodes;
    $('#fNewCode').value = '';
    renderEditBarcodes();
    await loadProducts(); await loadMissing(); await loadInternal();
  } catch (e) {
    $('#editErr').textContent = e.message === 'barcode_in_use'
      ? 'Ese código ya pertenece a otro producto.' : (e.message || 'No se pudo agregar.');
  }
}

/* --------------------------------------------------------------- wiring */
$$('#admNav button').forEach(b => b.onclick = () => switchView(b.dataset.view));
$('#testToggle').onclick = toggleTestMode;
$('#fltMissing').onclick = () => {
  S.filterMissing = !S.filterMissing;
  $('#fltMissing').classList.toggle('on', S.filterMissing);
  loadProducts();
};
$('#newProduct').onclick = () => openEdit(null);
$('#editClose').onclick = closeEdit;
$('#editDelete').onclick = async () => {
  if (!S.editing || !S.editing.id) return;
  const name = S.editing.name;
  if (!confirm(`¿Eliminar "${name}" del catálogo?\n\nEsto no se puede deshacer.`)) return;
  try {
    await api(`/api/admin/products/${S.editing.id}`, { method: 'DELETE' });
    await loadProducts(); await loadMissing(); await loadInternal();
    closeEdit();
    toast(`Eliminado: ${name}`);
  } catch (e) {
    if (e.status === 409) {
      // Sold at least once. Its sale_line rows point at this id, and stock is
      // derived as received - sold, so removing it would corrupt both history
      // and reports. Deactivating is the right answer and does what the user
      // actually wants: it disappears from the till.
      if (confirm(`"${name}" ya tiene ventas registradas, así que no se puede eliminar `
                + `sin dañar el historial y los reportes.\n\n¿Desactivarlo? Dejará de `
                + `aparecer en la caja, pero conserva su historial.`)) {
        await api(`/api/admin/products/${S.editing.id}`, { method: 'PUT',
          body: JSON.stringify({ is_active: false }) });
        await loadProducts(); await loadMissing(); await loadInternal();
        closeEdit();
        toast(`Desactivado: ${name}`);
      }
    } else {
      $('#editErr').textContent = e.message || 'No se pudo eliminar.';
    }
  }
};
$('#editCancel').onclick = closeEdit;
$('#editSave').onclick = saveEdit;
$('#addCode').onclick = addManualCode;
$('#genCode').onclick = async () => {
  if (!S.editing || !S.editing.id) {
    S.pendingGen = true;
    $('#editErr').textContent = '';
    renderEditBarcodes();
    return;
  }
  const updated = await api(`/api/admin/products/${S.editing.id}/generate_barcode`, { method: 'POST' });
  S.editing.barcodes = updated.barcodes;
  renderEditBarcodes();
  await loadProducts(); await loadMissing(); await loadInternal();
};
$('#printSheet').onclick = () => window.print();

boot().catch(e => { console.error(e); showFatalError('Error al iniciar', e); });
