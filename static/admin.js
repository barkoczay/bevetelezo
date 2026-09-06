/* -------------------------------------------------------------------------
   Bevételező — admin felület

   A raktáros beolvasása után itt történik az átnézés, javítás és az
   Excel export. Az export a lezárás pillanata: onnantól a bevételezés
   nem szerkeszthető, és a rendelések maradéka csökken.
   ------------------------------------------------------------------------- */

'use strict';

const API = '/api';
const $ = (id) => document.getElementById(id);

const state = {
  token: localStorage.getItem('token') || null,
  userName: localStorage.getItem('userName') || '',
  suppliers: [],
  receipt: null,
  order: null,
  pendingUpload: null,
  // Lapozás: egyszerre ennyit töltünk, a többit kérésre.
  pageSize: 50,
  receiptOffset: 0,
  orderOffset: 0,
  selectedReceipts: new Set(),
  selectedOrders: new Set(),
};

/* --- API ---------------------------------------------------------------- */

async function api(path, { method = 'GET', body, form, raw } = {}) {
  const headers = {};
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (body) headers['Content-Type'] = 'application/json';

  const res = await fetch(API + path, {
    method,
    headers,
    body: form || (body ? JSON.stringify(body) : undefined),
  });

  if (res.status === 401) { logout(); throw new Error('Lejárt a bejelentkezés.'); }
  if (!res.ok) {
    let detail = `Hiba (${res.status})`;
    try {
      const data = await res.json();
      if (typeof data.detail === 'string') detail = data.detail;
    } catch (_) { /* nem JSON */ }
    throw new Error(detail);
  }
  if (raw) return res;
  return res.status === 204 ? null : res.json();
}

function note(id, message, kind = 'error') {
  const el = $(id);
  el.className = `note note--${kind}`;
  el.textContent = message || '';
}

/* --- formázás ----------------------------------------------------------- */

const numberFormat = new Intl.NumberFormat('hu-HU', { maximumFractionDigits: 3 });
const moneyFormat = new Intl.NumberFormat('hu-HU', { maximumFractionDigits: 2 });

function qty(value) {
  return numberFormat.format(Number(value ?? 0));
}

/* Szerkesztéshez: a felesleges tizedesnullák nélkül (19444.0000 -> 19444) */
function priceValue(value) {
  if (value === null || value === undefined || value === '') return '';
  const number = Number(value);
  return Number.isFinite(number) ? String(number) : '';
}

function money(value) {
  return value === null || value === undefined ? '—' : moneyFormat.format(Number(value));
}

function date(value) {
  if (!value) return '—';
  return new Date(value).toLocaleDateString('hu-HU');
}

function dateTime(value) {
  if (!value) return '—';
  return new Date(value).toLocaleString('hu-HU', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  });
}

function element(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}

function table(headers, rows) {
  const t = element('table');
  const thead = element('thead');
  const tr = element('tr');
  for (const h of headers) {
    const th = element('th', h.className, h.label ?? h);
    tr.appendChild(th);
  }
  thead.appendChild(tr);
  t.appendChild(thead);
  const tbody = element('tbody');
  rows.forEach((r) => tbody.appendChild(r));
  t.appendChild(tbody);
  return t;
}

/* Kijelölés a listákban. A fejléc négyzete csak a betöltött sorokra
   vonatkozik — nem jelöl ki olyat, amit a felhasználó nem lát. */
function checkboxCell(set, id, prefix) {
  const cell = element('td', 'check-col');
  const box = document.createElement('input');
  box.type = 'checkbox';
  box.className = 'check';
  box.checked = set.has(id);
  box.addEventListener('click', (e) => e.stopPropagation());
  box.addEventListener('change', () => {
    if (box.checked) set.add(id); else set.delete(id);
    box.closest('tr').classList.toggle('is-selected', box.checked);
    updateBulkBar(prefix, set);
  });
  cell.appendChild(box);
  return cell;
}

function headerCheckbox(set, prefix) {
  const box = document.createElement('input');
  box.type = 'checkbox';
  box.className = 'check';
  box.addEventListener('change', () => {
    document.querySelectorAll(`#${prefix}-list tbody .check`).forEach((cb) => {
      if (cb.checked !== box.checked) cb.click();
    });
  });
  return box;
}

function updateBulkBar(prefix, set) {
  const bar = $(`${prefix}-bulk`);
  bar.hidden = set.size === 0;
  $(`${prefix}-selected`).textContent = `${set.size} kijelölve`;
}

function clearSelection(prefix, set) {
  set.clear();
  document.querySelectorAll(`#${prefix}-list .check`).forEach((cb) => { cb.checked = false; });
  document.querySelectorAll(`#${prefix}-list tr`).forEach((tr) => tr.classList.remove('is-selected'));
  updateBulkBar(prefix, set);
}

async function bulkDelete(prefix, set, path, question) {
  if (!set.size) return;
  if (!confirm(`${set.size} tétel: ${question}`)) return;

  try {
    const result = await api(path, { method: 'POST', body: { ids: [...set] } });
    set.clear();
    updateBulkBar(prefix, set);

    let message = `${result.deleted} törölve.`;
    if (result.skipped.length) {
      const reasons = result.skipped
        .map((s) => `${s.label}: ${s.reason}`)
        .join('; ');
      message += ` ${result.skipped.length} kihagyva — ${reasons}`;
    }
    note(`${prefix}-note`, message, result.skipped.length ? 'warn' : 'ok');
    return true;
  } catch (err) {
    note(`${prefix}-note`, err.message);
    return false;
  }
}

const RECEIPT_STATUS = {
  in_progress: ['Folyamatban', 'open'],
  scanned: ['Beolvasva', 'partial'],
  exported: ['Exportálva', 'closed'],
};

const ORDER_STATUS = {
  open: ['Nyitott', 'open'],
  partial: ['Részben teljesített', 'partial'],
  closed: ['Lezárt', 'closed'],
};

function tag(map, key) {
  const [label, kind] = map[key] || [key, 'open'];
  return element('span', `tag tag--${kind}`, label);
}

/* --- bejelentkezés ------------------------------------------------------ */

async function login() {
  note('login-error', '');
  const username = $('login-user').value.trim();
  const password = $('login-pass').value;
  if (!username || !password) {
    note('login-error', 'Add meg a felhasználónevet és a jelszót.');
    return;
  }
  try {
    const res = await fetch(`${API}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ username, password }),
    });
    if (!res.ok) throw new Error('Hibás felhasználónév vagy jelszó.');
    const data = await res.json();
    state.token = data.access_token;
    state.userName = data.display_name;
    localStorage.setItem('token', state.token);
    localStorage.setItem('userName', state.userName);
    await enterApp();
  } catch (err) {
    note('login-error', err.message);
  }
}

function logout() {
  state.token = null;
  localStorage.removeItem('token');
  localStorage.removeItem('userName');
  $('app').hidden = true;
  $('login-wrap').hidden = false;
}

async function enterApp() {
  $('login-wrap').hidden = true;
  $('app').hidden = false;
  $('who').textContent = state.userName;
  await loadSuppliers();
  await loadReceipts();
}

/* --- lapfülek ----------------------------------------------------------- */

function showPanel(name) {
  document.querySelectorAll('.panel').forEach((p) => p.classList.remove('is-active'));
  $('panel-' + name).classList.add('is-active');
  document.querySelectorAll('.tab').forEach((t) => {
    t.classList.toggle('is-active', t.dataset.panel === name);
  });
}

/* --- szállítók ---------------------------------------------------------- */

async function loadSuppliers() {
  state.suppliers = await api('/suppliers?include_inactive=true');
  renderSuppliers();
  fillSupplierSelects();
}

function fillSupplierSelects() {
  for (const id of ['receipt-supplier-filter', 'order-supplier-filter']) {
    const select = $(id);
    const current = select.value;
    select.innerHTML = '<option value="">Mind</option>';
    for (const s of state.suppliers) {
      const option = document.createElement('option');
      option.value = s.id;
      option.textContent = s.name;
      select.appendChild(option);
    }
    if (current) select.value = current;
  }

  for (const id of ['order-supplier', 'od-supplier']) {
    const select = $(id);
    const current = select.value;
    select.innerHTML = '';
    for (const s of state.suppliers.filter((x) => x.active)) {
      const option = document.createElement('option');
      option.value = s.id;
      option.textContent = s.name;
      select.appendChild(option);
    }
    if (current) select.value = current;
  }
}

function renderSuppliers() {
  const rows = state.suppliers.map((s) => {
    const tr = element('tr');
    tr.appendChild(element('td', null, s.name));
    tr.appendChild(element('td', null, s.active ? 'Aktív' : 'Inaktív'));

    const actions = element('td');
    const toggle = element('button', 'btn btn--small', s.active ? 'Inaktiválás' : 'Aktiválás');
    toggle.addEventListener('click', async () => {
      try {
        await api(`/suppliers/${s.id}`, {
          method: 'PATCH',
          body: { name: s.name, active: !s.active },
        });
        await loadSuppliers();
      } catch (err) { note('supplier-note', err.message); }
    });
    actions.appendChild(toggle);
    tr.appendChild(actions);
    return tr;
  });

  const host = $('supplier-list');
  host.innerHTML = '';
  if (!rows.length) {
    host.appendChild(element('p', 'empty', 'Még nincs szállító. Vegyél fel egyet fent.'));
    return;
  }
  host.appendChild(table(['Név', 'Állapot', ''], rows));
}

async function addSupplier() {
  const name = $('supplier-name').value.trim();
  if (!name) return;
  note('supplier-note', '');
  try {
    await api('/suppliers', { method: 'POST', body: { name, active: true } });
    $('supplier-name').value = '';
    await loadSuppliers();
    note('supplier-note', `${name} felvéve.`, 'ok');
  } catch (err) { note('supplier-note', err.message); }
}

/* --- bevételezések ------------------------------------------------------ */

async function loadReceipts(append = false) {
  if (!append) {
    note('receipt-note', '');
    state.receiptOffset = 0;
    state.selectedReceipts.clear();
    updateBulkBar('receipt', state.selectedReceipts);
  }

  const params = new URLSearchParams({
    limit: String(state.pageSize),
    offset: String(state.receiptOffset),
  });
  if ($('receipt-status').value) params.set('status', $('receipt-status').value);
  if ($('receipt-supplier-filter').value) {
    params.set('supplier_id', $('receipt-supplier-filter').value);
  }

  try {
    const receipts = await api(`/receipts?${params}`);
    renderReceipts(receipts, append);
    state.receiptOffset += receipts.length;
    // Ha teli oldalt kaptunk, valószínűleg van még.
    $('receipt-more').hidden = receipts.length < state.pageSize;
  } catch (err) { note('receipt-note', err.message); }
}

function renderReceipts(receipts, append = false) {
  const host = $('receipt-list');
  const existing = append ? host.querySelector('tbody') : null;
  if (!append) host.innerHTML = '';

  if (!receipts.length) {
    if (!append) host.appendChild(element('p', 'empty', 'Nincs megjeleníthető bevételezés.'));
    return;
  }

  const rows = receipts.map((r) => {
    const tr = element('tr', 'clickable');
    const alert = r.missing_in_naturasoft_count > 0 || r.unknown_count > 0;
    tr.classList.add(
      r.status === 'exported' ? 'state-closed'
        : alert ? 'state-alert'
          : r.status === 'scanned' ? 'state-partial' : 'state-open'
    );

    tr.appendChild(checkboxCell(state.selectedReceipts, r.id, 'receipt'));
    if (state.selectedReceipts.has(r.id)) tr.classList.add('is-selected');
    tr.appendChild(element('td', null, dateTime(r.created_at)));
    tr.appendChild(element('td', null, r.supplier_name || '—'));

    const statusCell = element('td');
    statusCell.appendChild(tag(RECEIPT_STATUS, r.status));
    tr.appendChild(statusCell);

    tr.appendChild(element('td', 'num', String(r.item_count)));
    tr.appendChild(element('td', null, r.reference_number || r.suggested_reference || '—'));

    const flags = element('td');
    if (r.missing_in_naturasoft_count) {
      flags.appendChild(element('span', 'tag tag--alert',
        `${r.missing_in_naturasoft_count} nincs a Naturasoftban`));
    }
    if (r.unknown_count) {
      flags.appendChild(element('span', 'tag tag--partial',
        `${r.unknown_count} félretéve`));
    }
    tr.appendChild(flags);

    tr.addEventListener('click', () => openReceipt(r.id));
    return tr;
  });

  if (existing) {
    rows.forEach((r) => existing.appendChild(r));
    return;
  }

  const t = table(
    [{ label: '', className: 'check-col' }, 'Létrehozva', 'Szállító', 'Állapot',
     { label: 'Tétel', className: 'num' }, 'Hivatkozás', 'Jelzések'],
    rows,
  );
  t.querySelector('thead th').appendChild(headerCheckbox(state.selectedReceipts, 'receipt'));
  host.appendChild(t);
}

async function openReceipt(id) {
  note('rd-note', '');
  try {
    state.receipt = await api(`/receipts/${id}`);
    renderReceiptDetail();
    showPanel('receipt');
  } catch (err) { note('receipt-note', err.message); }
}

function renderReceiptDetail() {
  const r = state.receipt;
  const editable = r.status !== 'exported';

  $('rd-title').textContent = `Bevételezés — ${r.supplier_name || 'ismeretlen szállító'}`;

  const facts = $('rd-facts');
  facts.innerHTML = '';
  const addFact = (label, value) => {
    const box = element('div');
    box.appendChild(element('div', 'fact__label', label));
    box.appendChild(element('div', 'fact__value', value));
    facts.appendChild(box);
  };
  addFact('Állapot', RECEIPT_STATUS[r.status]?.[0] || r.status);
  addFact('Létrehozva', dateTime(r.created_at));
  addFact('Beolvasás vége', dateTime(r.scanned_at));
  if (r.exported_at) addFact('Exportálva', dateTime(r.exported_at));
  addFact('Tételek', String(r.item_count));

  $('rd-reference').value = r.reference_number || r.suggested_reference || '';
  $('rd-reference').disabled = !editable;
  $('rd-save-ref').disabled = !editable;
  $('rd-export').disabled = !editable;
  $('rd-export').textContent = editable ? 'Excel letöltése' : 'Már exportálva';

  // Visszaadás csak akkor, ha a raktáros már befejezte — folyamatban
  // lévőt nem kell visszaadni.
  $('rd-reopen').hidden = r.status !== 'scanned';
  $('rd-delete').hidden = !editable;

  const missingPrice = r.items.filter(
    (i) => i.net_unit_price === null || i.net_unit_price === undefined).length;

  if (!editable) {
    note('rd-note',
      'Ez a bevételezés lezárult az export után, ezért nem módosítható. Ha javítani kell, azt a Naturasoftban tedd meg.',
      'warn');
  } else if (missingPrice) {
    note('rd-note',
      `${missingPrice} tételnél hiányzik a beszerzési ár. Ezek nulla forinttal kerülnének a Naturasoftba — írd be az árat az export előtt.`,
      'warn');
  } else if (r.missing_in_naturasoft_count) {
    note('rd-note',
      `${r.missing_in_naturasoft_count} tétel valószínűleg nincs a Naturasoftban. Ezeket előbb ott kell létrehozni, különben az import kihagyja őket.`,
      'warn');
  }

  renderReceiptItems(editable);
  renderUnknownScans();
}

function renderReceiptItems(editable) {
  const host = $('rd-items');
  host.innerHTML = '';
  const items = state.receipt.items;

  if (!items.length) {
    host.appendChild(element('p', 'empty', 'Ez a bevételezés nem tartalmaz tételt.'));
    return;
  }

  const rows = items.map((item) => {
    const tr = element('tr');
    if (item.missing_in_naturasoft) tr.classList.add('row-alert');
    else if (item.source === 'outside_order') tr.classList.add('row-warn');

    tr.appendChild(element('td', 'wrap', item.name_snapshot));
    tr.appendChild(element('td', null, item.sku_snapshot));
    tr.appendChild(element('td', null,
      item.order_number ? item.order_number : 'rendelésen kívüli'));

    const qtyCell = element('td', 'num');
    if (editable) {
      const input = element('input', 'qty-input');
      input.value = qty(item.qty);
      input.addEventListener('change', () => saveItemQty(item, input));
      qtyCell.appendChild(input);
    } else {
      qtyCell.textContent = qty(item.qty);
    }
    tr.appendChild(qtyCell);

    const priceCell = element('td', 'num');
    if (editable) {
      const input = element('input', 'qty-input price-input');
      input.value = priceValue(item.net_unit_price);
      input.placeholder = 'nincs ár';
      // Ár nélküli tétel 0 forinttal menne a Naturasoftba — ezt jelezzük.
      if (item.net_unit_price === null || item.net_unit_price === undefined) {
        input.classList.add('is-missing');
      }
      input.addEventListener('change', () => saveItemPrice(item, input));
      priceCell.appendChild(input);
    } else {
      priceCell.textContent = money(item.net_unit_price);
    }
    tr.appendChild(priceCell);

    const actions = element('td');
    if (editable) {
      const remove = element('button', 'btn btn--small btn--danger', 'Törlés');
      remove.addEventListener('click', () => deleteItem(item));
      actions.appendChild(remove);
    }
    tr.appendChild(actions);
    return tr;
  });

  host.appendChild(table(
    ['Megnevezés', 'Cikkszám', 'Megrendelés',
     { label: 'Mennyiség', className: 'num' },
     { label: 'Nettó ár', className: 'num' }, ''],
    rows,
  ));
}

function renderUnknownScans() {
  const scans = state.receipt.unknown_scans || [];
  $('rd-unknown-title').hidden = !scans.length;
  $('rd-unknown-card').hidden = !scans.length;
  if (!scans.length) return;

  const rows = scans.map((s) => {
    const tr = element('tr');
    tr.appendChild(element('td', null, s.raw_code));
    tr.appendChild(element('td', null, dateTime(s.scanned_at)));
    return tr;
  });
  const host = $('rd-unknown');
  host.innerHTML = '';
  host.appendChild(table(['Beolvasott kód', 'Időpont'], rows));
}

async function saveItemQty(item, input) {
  const value = Number(input.value.replace(',', '.'));
  if (!Number.isFinite(value) || value <= 0) {
    input.value = qty(item.qty);
    note('rd-note', 'A mennyiségnek nullánál nagyobb számnak kell lennie.');
    return;
  }
  try {
    await api(`/receipts/${state.receipt.id}/items/${item.id}`, {
      method: 'PATCH',
      body: { qty: value },
    });
    await openReceipt(state.receipt.id);
  } catch (err) { note('rd-note', err.message); }
}

async function saveItemPrice(item, input) {
  const raw = input.value.trim().replace(/\s/g, '').replace(',', '.');
  if (!raw) {
    note('rd-note', 'Az ár nem lehet üres — ilyenkor 0 forinttal kerülne a Naturasoftba.');
    input.value = priceValue(item.net_unit_price);
    return;
  }
  const value = Number(raw);
  if (!Number.isFinite(value) || value < 0) {
    input.value = priceValue(item.net_unit_price);
    note('rd-note', 'Az árnak számnak kell lennie.');
    return;
  }
  try {
    await api(`/receipts/${state.receipt.id}/items/${item.id}`, {
      method: 'PATCH',
      body: { net_unit_price: value },
    });
    await openReceipt(state.receipt.id);
  } catch (err) { note('rd-note', err.message); }
}

async function deleteItem(item) {
  if (!confirm(`Törlöd ezt a tételt?\n\n${item.name_snapshot}`)) return;
  try {
    await api(`/receipts/${state.receipt.id}/items/${item.id}`, { method: 'DELETE' });
    await openReceipt(state.receipt.id);
  } catch (err) { note('rd-note', err.message); }
}

async function saveReference() {
  try {
    await api(`/receipts/${state.receipt.id}`, {
      method: 'PATCH',
      body: { reference_number: $('rd-reference').value.trim() },
    });
    note('rd-note', 'Hivatkozási szám mentve.', 'ok');
  } catch (err) { note('rd-note', err.message); }
}

async function reopenReceipt() {
  if (!confirm(
    'Visszaadod a raktárosnak? A bevételezés újra megnyílik, és folytatható a beolvasás.'
  )) return;
  try {
    await api(`/receipts/${state.receipt.id}/reopen`, { method: 'POST' });
    await openReceipt(state.receipt.id);
    note('rd-note', 'Visszaadva. A raktáros folytathatja a beolvasást.', 'ok');
  } catch (err) { note('rd-note', err.message); }
}

async function deleteReceipt() {
  if (!confirm(
    'Törlöd ezt a bevételezést?\n\nA tételei visszakerülnek a megrendelések maradékába. A művelet nem vonható vissza.'
  )) return;
  try {
    await api(`/receipts/${state.receipt.id}`, { method: 'DELETE' });
    showPanel('receipts');
    await loadReceipts();
  } catch (err) { note('rd-note', err.message); }
}

async function exportReceipt() {
  if (!confirm(
    'A letöltés után ez a bevételezés lezárul, és nem lesz módosítható.\n\n' +
    'A megrendelés nyitva marad: ami még nem érkezett meg, azt később egy új bevételezésben lehet felvenni.\n\nFolytatod?'
  )) return;

  try {
    const res = await api(`/receipts/${state.receipt.id}/export`, {
      method: 'POST', raw: true,
    });
    const blob = await res.blob();
    const disposition = res.headers.get('content-disposition') || '';
    const match = disposition.match(/filename="?([^"]+)"?/);
    const filename = match ? match[1] : 'bevetelezes.xls';

    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(url);

    await openReceipt(state.receipt.id);
    note('rd-note',
      `${filename} letöltve. Ez a bevételezés lezárult — a megrendelés maradékát egy új bevételezésben lehet folytatni.`,
      'ok');
  } catch (err) { note('rd-note', err.message); }
}

/* --- megrendelések ------------------------------------------------------ */

async function loadOrders(append = false) {
  if (!append) {
    note('order-note', '');
    state.orderOffset = 0;
    state.selectedOrders.clear();
    updateBulkBar('order', state.selectedOrders);
  }

  const params = new URLSearchParams({
    limit: String(state.pageSize),
    offset: String(state.orderOffset),
  });
  if ($('order-status').value) params.set('status', $('order-status').value);
  if ($('order-search').value.trim()) params.set('q', $('order-search').value.trim());
  if ($('order-supplier-filter').value) {
    params.set('supplier_id', $('order-supplier-filter').value);
  }

  try {
    const orders = await api(`/orders?${params}`);
    renderOrders(orders, append);
    state.orderOffset += orders.length;
    $('order-more').hidden = orders.length < state.pageSize;
  } catch (err) { note('order-note', err.message); }
}

function renderOrders(orders, append = false) {
  const host = $('order-list');
  const existing = append ? host.querySelector('tbody') : null;
  if (!append) host.innerHTML = '';

  if (!orders.length) {
    if (!append) host.appendChild(element('p', 'empty', 'Nincs megjeleníthető megrendelés.'));
    return;
  }

  const rows = orders.map((o) => {
    const tr = element('tr', 'clickable');
    tr.classList.add(`state-${o.status}`);

    tr.appendChild(checkboxCell(state.selectedOrders, o.id, 'order'));
    if (state.selectedOrders.has(o.id)) tr.classList.add('is-selected');
    tr.appendChild(element('td', null, o.order_number));
    tr.appendChild(element('td', null, date(o.order_date)));
    tr.appendChild(element('td', null, o.supplier_name || '—'));

    const statusCell = element('td');
    statusCell.appendChild(tag(ORDER_STATUS, o.status));
    if (o.closed_manually) statusCell.appendChild(element('span', 'muted', ' kézzel'));
    tr.appendChild(statusCell);

    // A beolvasás azonnal bevételezésnek számít, ezért egy sáv elég.
    const ordered = Number(o.ordered_total) || 0;
    const received = Number(o.received_total) || 0;
    const percent = ordered ? Math.min(100, (received / ordered) * 100) : 0;

    const progressCell = element('td');
    const wrap = element('div', 'progress');
    const bar = element('div', 'progress__bar');
    const fill = element('div',
      `progress__fill${percent < 100 ? ' progress__fill--partial' : ''}`);
    fill.style.width = `${percent}%`;
    bar.appendChild(fill);
    wrap.appendChild(bar);
    wrap.appendChild(element('span', 'progress__text',
      `${qty(received)} / ${qty(ordered)}`));
    progressCell.appendChild(wrap);
    tr.appendChild(progressCell);

    tr.appendChild(element('td', 'num',
      `${o.completed_item_count} / ${o.item_count}`));

    tr.addEventListener('click', () => openOrder(o.id));
    return tr;
  });

  if (existing) {
    rows.forEach((r) => existing.appendChild(r));
    return;
  }

  const t = table(
    [{ label: '', className: 'check-col' }, 'Rendelésszám', 'Dátum', 'Szállító',
     'Állapot', 'Beérkezett', { label: 'Kész tétel', className: 'num' }],
    rows,
  );
  t.querySelector('thead th').appendChild(headerCheckbox(state.selectedOrders, 'order'));
  host.appendChild(t);
}

async function openOrder(id) {
  note('od-note', '');
  try {
    state.order = await api(`/orders/${id}`);
    renderOrderDetail();
    showPanel('order');
  } catch (err) { note('order-note', err.message); }
}

function renderOrderDetail() {
  const o = state.order;
  $('od-title').textContent = `Megrendelés ${o.order_number}`;

  const facts = $('od-facts');
  facts.innerHTML = '';
  const addFact = (label, value) => {
    const box = element('div');
    box.appendChild(element('div', 'fact__label', label));
    box.appendChild(element('div', 'fact__value', value));
    facts.appendChild(box);
  };
  addFact('Állapot', ORDER_STATUS[o.status]?.[0] || o.status);
  addFact('Szállító', o.supplier_name || '—');
  addFact('Raktár', o.warehouse || '—');
  addFact('Feltöltve', dateTime(o.uploaded_at));

  $('od-date').value = o.order_date;
  fillSupplierSelects();
  if (o.supplier_id) $('od-supplier').value = String(o.supplier_id);

  const closeBtn = $('od-close');
  // A megrendelés magától lezárul, ha minden megérkezik. Ez a gomb arra
  // való, amikor a maradék soha nem fog megjönni.
  closeBtn.textContent = o.status === 'closed'
    ? 'Újranyitás'
    : 'A maradék nem érkezik meg';
  closeBtn.hidden = o.status === 'closed' && !o.closed_manually;

  if (o.status === 'closed' && o.closed_manually) {
    note('od-note',
      'Ezt a megrendelést kézzel zárták le: a maradék már nem fog megérkezni.',
      'warn');
  }

  const rows = o.items.map((item) => {
    const tr = element('tr');
    const missing = Number(item.remaining_qty);

    if (missing <= 0) tr.classList.add('state-closed');
    else if (Number(item.received_qty) > 0) tr.classList.add('state-partial');
    else tr.classList.add('state-open');

    tr.appendChild(element('td', 'wrap', item.name_snapshot));
    tr.appendChild(element('td', null, item.sku_snapshot));
    tr.appendChild(element('td', 'num', qty(item.ordered_qty)));
    tr.appendChild(element('td', 'num', qty(item.received_qty)));

    const missingCell = element('td', 'num', qty(missing));
    if (missing <= 0) missingCell.classList.add('muted');
    tr.appendChild(missingCell);

    tr.appendChild(element('td', 'num', money(item.net_unit_price)));
    return tr;
  });

  const host = $('od-items');
  host.innerHTML = '';
  host.appendChild(table(
    ['Megnevezés', 'Cikkszám',
     { label: 'Rendelt', className: 'num' },
     { label: 'Érkezett', className: 'num' },
     { label: 'Még hiányzik', className: 'num' },
     { label: 'Nettó ár', className: 'num' }],
    rows,
  ));
}

async function saveOrder() {
  try {
    await api(`/orders/${state.order.id}`, {
      method: 'PATCH',
      body: {
        order_date: $('od-date').value,
        supplier_id: Number($('od-supplier').value) || null,
      },
    });
    await openOrder(state.order.id);
    note('od-note', 'Mentve.', 'ok');
  } catch (err) { note('od-note', err.message); }
}

async function toggleOrderClosed() {
  const closing = state.order.status !== 'closed';
  if (closing && !confirm(
    'Lezárod a megrendelést?\n\nEzt csak akkor tedd, ha a hiányzó tételek már nem fognak megérkezni. ' +
    'A rendszer onnantól nem könyvel rájuk bevételezést.'
  )) return;

  try {
    await api(`/orders/${state.order.id}/${closing ? 'close' : 'reopen'}`, { method: 'POST' });
    await openOrder(state.order.id);
  } catch (err) { note('od-note', err.message); }
}

async function deleteOrder() {
  if (!confirm(`Törlöd a(z) ${state.order.order_number} megrendelést?`)) return;
  try {
    await api(`/orders/${state.order.id}`, { method: 'DELETE' });
    showPanel('orders');
    await loadOrders();
  } catch (err) { note('od-note', err.message); }
}

/* --- megrendelés feltöltés ---------------------------------------------- */

async function previewOrder(file) {
  note('upload-note', '');
  const form = new FormData();
  form.append('file', file);
  try {
    const preview = await api('/orders/preview', { method: 'POST', form });
    state.pendingUpload = preview;

    if (preview.order_number) $('order-number').value = preview.order_number;
    if (!$('order-date').value) $('order-date').value = new Date().toISOString().slice(0, 10);

    const rows = preview.items.map((item) => {
      const tr = element('tr');
      tr.appendChild(element('td', 'wrap', item.name));
      tr.appendChild(element('td', null, item.sku));
      tr.appendChild(element('td', null, item.ean || '—'));
      tr.appendChild(element('td', 'num', qty(item.ordered_qty)));
      tr.appendChild(element('td', 'num', money(item.net_unit_price)));
      return tr;
    });

    const host = $('order-preview');
    host.innerHTML = '';
    host.appendChild(table(
      ['Megnevezés', 'Cikkszám', 'Vonalkód',
       { label: 'Mennyiség', className: 'num' },
       { label: 'Nettó ár', className: 'num' }],
      rows,
    ));
    $('preview-card').hidden = false;

    if (preview.already_exists) {
      note('upload-note',
        `A(z) ${preview.order_number} már fel van töltve. Új feltöltés csak akkor lehetséges, ha még nem történt rá bevételezés.`,
        'warn');
    } else {
      note('upload-note',
        `${preview.item_count} tétel a fájlban. Ellenőrizd a rendelésszámot és a dátumot, majd töltsd fel.`,
        'ok');
    }
  } catch (err) {
    $('preview-card').hidden = true;
    note('upload-note', err.message);
  }
}

async function uploadOrder() {
  const file = $('order-file').files[0];
  if (!file) { note('upload-note', 'Válaszd ki a fájlt.'); return; }
  if (!$('order-number').value.trim()) { note('upload-note', 'Add meg a rendelésszámot.'); return; }
  if (!$('order-date').value) { note('upload-note', 'Add meg a dátumot.'); return; }
  if (!$('order-supplier').value) { note('upload-note', 'Válassz szállítót.'); return; }

  const form = new FormData();
  form.append('file', file);
  form.append('order_number', $('order-number').value.trim());
  form.append('order_date', $('order-date').value);
  form.append('supplier_id', $('order-supplier').value);
  form.append('overwrite', String(Boolean(state.pendingUpload?.already_exists)));

  try {
    const order = await api('/orders/upload', { method: 'POST', form });

    const warnings = order.import_warnings || [];
    if (warnings.length) {
      note('upload-note',
        `${order.order_number} feltöltve, ${order.item_count} tétel. ` +
        `${warnings.length} figyelmeztetés: ${warnings.join(' | ')}`,
        'warn');
    } else {
      note('upload-note', `${order.order_number} feltöltve, ${order.item_count} tétel.`, 'ok');
    }
    $('order-file').value = '';
    $('order-number').value = '';
    $('preview-card').hidden = true;
    state.pendingUpload = null;
    await loadOrders();
  } catch (err) { note('upload-note', err.message); }
}

/* --- terméktörzs -------------------------------------------------------- */

async function importProducts() {
  const file = $('product-file').files[0];
  if (!file) { note('product-note', 'Válaszd ki a fájlt.'); return; }

  note('product-note', 'Import folyamatban…', 'ok');
  $('product-warnings').innerHTML = '';

  const form = new FormData();
  form.append('file', file);
  try {
    const result = await api('/products/import', { method: 'POST', form });
    note('product-note',
      `${result.rows_total} sor feldolgozva: ${result.created} új, ${result.updated} frissítve, ${result.skipped} kihagyva.`,
      'ok');

    const host = $('product-warnings');
    host.innerHTML = '';
    if (result.warnings.length) {
      host.appendChild(element('div',
        null, `${result.warnings.length} figyelmeztetés:`));
      for (const w of result.warnings.slice(0, 300)) {
        host.appendChild(element('div', null, w));
      }
    }
    $('product-file').value = '';
  } catch (err) { note('product-note', err.message); }
}

let searchTimer = null;

async function searchProducts(term) {
  const host = $('product-results');
  if (term.length < 2) {
    host.innerHTML = '';
    host.appendChild(element('p', 'empty', 'Írd be a keresett nevet vagy cikkszámot.'));
    return;
  }
  try {
    const results = await api(`/products/search?q=${encodeURIComponent(term)}&limit=50`);
    host.innerHTML = '';
    if (!results.length) {
      host.appendChild(element('p', 'empty', 'Nincs találat.'));
      return;
    }
    const rows = results.map((p) => {
      const tr = element('tr');
      if (!p.in_naturasoft) tr.classList.add('row-alert');
      tr.appendChild(element('td', 'wrap', p.name));
      tr.appendChild(element('td', null, p.sku));
      tr.appendChild(element('td', null, p.ean || '—'));
      tr.appendChild(element('td', null, p.unit));
      tr.appendChild(element('td', null,
        p.in_naturasoft ? '' : 'nincs a Naturasoftban'));
      return tr;
    });
    host.appendChild(table(
      ['Megnevezés', 'Cikkszám', 'Vonalkód', 'Egység', ''], rows));
  } catch (err) {
    host.innerHTML = '';
    host.appendChild(element('p', 'empty', err.message));
  }
}

/* --- események ---------------------------------------------------------- */

$('login-btn').addEventListener('click', login);
$('login-pass').addEventListener('keydown', (e) => { if (e.key === 'Enter') login(); });
$('logout-btn').addEventListener('click', logout);

document.querySelectorAll('.tab').forEach((tabButton) => {
  tabButton.addEventListener('click', () => {
    const name = tabButton.dataset.panel;
    showPanel(name);
    if (name === 'receipts') loadReceipts();
    if (name === 'orders') loadOrders();
    if (name === 'suppliers') loadSuppliers();
  });
});

$('receipt-refresh').addEventListener('click', () => loadReceipts());
$('receipt-status').addEventListener('change', () => loadReceipts());
$('receipt-supplier-filter').addEventListener('change', () => loadReceipts());
$('receipt-more').addEventListener('click', () => loadReceipts(true));
$('receipt-bulk-clear').addEventListener('click',
  () => clearSelection('receipt', state.selectedReceipts));
$('receipt-bulk-delete').addEventListener('click', async () => {
  const done = await bulkDelete('receipt', state.selectedReceipts, '/receipts/bulk-delete',
    'törlöd? A tételek mennyisége visszakerül a megrendelésekbe. Exportált bevételezés nem törölhető.');
  if (done) {
    const message = $('receipt-note').textContent;
    await loadReceipts();
    note('receipt-note', message, 'ok');
  }
});
$('receipt-back').addEventListener('click', () => { showPanel('receipts'); loadReceipts(); });
$('rd-save-ref').addEventListener('click', saveReference);
$('rd-export').addEventListener('click', exportReceipt);
$('rd-reopen').addEventListener('click', reopenReceipt);
$('rd-delete').addEventListener('click', deleteReceipt);

$('order-refresh').addEventListener('click', () => loadOrders());
$('order-status').addEventListener('change', () => loadOrders());
$('order-supplier-filter').addEventListener('change', () => loadOrders());
$('order-more').addEventListener('click', () => loadOrders(true));
$('order-bulk-clear').addEventListener('click',
  () => clearSelection('order', state.selectedOrders));
$('order-bulk-delete').addEventListener('click', async () => {
  const done = await bulkDelete('order', state.selectedOrders, '/orders/bulk-delete',
    'törlöd? Amelyikhez már tartozik bevételezés, azt kihagyjuk.');
  if (done) {
    const message = $('order-note').textContent;
    await loadOrders();
    note('order-note', message, 'ok');
  }
});
$('order-search').addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadOrders(), 300);
});
$('order-back').addEventListener('click', () => { showPanel('orders'); loadOrders(); });
$('order-file').addEventListener('change', (e) => {
  if (e.target.files[0]) previewOrder(e.target.files[0]);
});
$('order-upload').addEventListener('click', uploadOrder);
$('od-save').addEventListener('click', saveOrder);
$('od-close').addEventListener('click', toggleOrderClosed);
$('od-delete').addEventListener('click', deleteOrder);

$('supplier-add').addEventListener('click', addSupplier);
$('supplier-name').addEventListener('keydown', (e) => { if (e.key === 'Enter') addSupplier(); });

$('product-import').addEventListener('click', importProducts);
$('product-search').addEventListener('input', (e) => {
  clearTimeout(searchTimer);
  const term = e.target.value.trim();
  searchTimer = setTimeout(() => searchProducts(term), 250);
});

/* --- indulás ------------------------------------------------------------ */

if (state.token) {
  enterApp().catch(() => logout());
} else {
  $('app').hidden = true;
  $('login-wrap').hidden = false;
}
