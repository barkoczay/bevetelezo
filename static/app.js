/* -------------------------------------------------------------------------
   Raktári bevételező — raktáros felület

   Három bevitel, egyenrangúan: Bluetooth vonalkódolvasó, hang, kézi keresés.
   A raktáros csak a termék nevét és a mennyiséget látja — hogy melyik
   rendelésre könyvelődik, az nem az ő dolga.
   ------------------------------------------------------------------------- */

'use strict';

const API = '/api';
const $ = (id) => document.getElementById(id);

const state = {
  token: localStorage.getItem('token') || null,
  userName: localStorage.getItem('userName') || '',
  receiptId: null,
  supplierName: '',
  items: [],          // { productId, name, unit, qty }
  lastProductCode: null,
  lastProductId: null,
  /* A termék darabszáma a MOSTANI beolvasás ELŐTT. A bemondott mennyiség
     ehhez adódik, nem a teljes darabszámot állítja be — így ha ugyanazt a
     vonalkódot kétszer olvassák be, a második mennyiség hozzáadódik. */
  quantityBase: 0,
  /* A hangvezérlés két lépésben halad:
       'product'  — terméket keresünk (a keresőszó tartalmazhat számot)
       'quantity' — megvan a termék, a darabszámot várjuk
     Így nem kell találgatni, hogy a '250' mennyiség-e vagy típusszám. */
  step: 'product',
};

/* --- API ---------------------------------------------------------------- */

async function api(path, { method = 'GET', body, form } = {}) {
  const headers = {};
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (body) headers['Content-Type'] = 'application/json';

  const res = await fetch(API + path, {
    method,
    headers,
    body: form ? form : body ? JSON.stringify(body) : undefined,
  });

  if (res.status === 401) {
    logout();
    throw new Error('Lejárt a bejelentkezés. Lépj be újra.');
  }
  if (!res.ok) {
    let detail = `Hiba (${res.status})`;
    try {
      const data = await res.json();
      if (data.detail) detail = typeof data.detail === 'string' ? data.detail : detail;
    } catch (_) { /* nem JSON válasz */ }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

/* --- képernyőváltás ----------------------------------------------------- */

function show(name) {
  document.querySelectorAll('.screen').forEach((s) => s.classList.remove('is-active'));
  $('screen-' + name).classList.add('is-active');

  // A képernyő csak a munka alatt maradjon ébren — a keresés is ide
  // tartozik, mert onnan lép vissza a szkennelésre.
  if (name === 'scan' || name === 'search') keepScreenAwake();
  else releaseScreenLock();

  if (name === 'scan') focusScanner();
  if (name === 'search') setTimeout(() => $('search-input').focus(), 50);
}

/* --- bejelentkezés ------------------------------------------------------ */

async function login() {
  const username = $('login-user').value.trim();
  const password = $('login-pass').value;
  $('login-error').textContent = '';

  if (!username || !password) {
    $('login-error').textContent = 'Add meg a felhasználónevet és a jelszót.';
    return;
  }

  const form = new URLSearchParams({ username, password });
  try {
    const res = await fetch(`${API}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: form,
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || 'Sikertelen bejelentkezés.');
    }
    const data = await res.json();
    state.token = data.access_token;
    state.userName = data.display_name;
    localStorage.setItem('token', state.token);
    localStorage.setItem('userName', state.userName);
    $('login-pass').value = '';
    await openStart();
  } catch (err) {
    $('login-error').textContent = err.message;
  }
}

function logout() {
  voice.stop();
  if (synth) synth.cancel();
  state.token = null;
  state.receiptId = null;
  localStorage.removeItem('token');
  localStorage.removeItem('userName');
  show('login');
}

/* --- indítás ------------------------------------------------------------ */

async function openStart() {
  $('start-error').textContent = '';
  try {
    await renderResumable();
    const suppliers = await api('/suppliers');
    const select = $('supplier');
    select.innerHTML = '';
    if (!suppliers.length) {
      $('start-error').textContent =
        'Nincs felvéve szállító. Szólj az adminnak, hogy vegye fel.';
    }
    for (const s of suppliers) {
      const opt = document.createElement('option');
      opt.value = s.id;
      opt.textContent = s.name;
      select.appendChild(opt);
    }
    show('start');
  } catch (err) {
    $('start-error').textContent = err.message;
  }
}

/* Félbehagyott bevételezések. Enélkül minden megszakadt munka (lemerült
   telefon, véletlen kilépés) új bevételezést kényszerítene, a régi pedig
   ott maradna befejezetlenül. */
async function renderResumable() {
  const box = $('resume-box');
  const list = $('resume-list');
  list.innerHTML = '';

  let open = [];
  try {
    open = await api('/receipts?status=in_progress');
  } catch (_) {
    box.hidden = true;
    return;
  }

  box.hidden = open.length === 0;

  for (const receipt of open) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'resume';
    btn.innerHTML = '<span class="resume__name"></span><span class="resume__meta"></span>';
    btn.querySelector('.resume__name').textContent = receipt.supplier_name || 'ismeretlen szállító';
    btn.querySelector('.resume__meta').textContent = `${receipt.item_count} tétel`;
    btn.addEventListener('click', () => resumeReceipt(receipt.id));
    list.appendChild(btn);
  }
}

async function resumeReceipt(id) {
  $('start-error').textContent = '';
  try {
    const receipt = await api(`/receipts/${id}`);
    state.receiptId = receipt.id;
    state.supplierName = receipt.supplier_name || '';
    state.lastProductCode = null;
    state.step = 'product';

    // A korábban beolvasott tételek visszatöltése, termékenként összevonva
    // (a FIFO több sorra bonthatta ugyanazt a terméket).
    const byProduct = new Map();
    for (const item of receipt.items) {
      const code = item.ean_snapshot || item.sku_snapshot;
      const existing = byProduct.get(code);
      if (existing) existing.qty += Number(item.qty);
      else byProduct.set(code, {
        productId: code,
        name: item.name_snapshot,
        unit: item.unit,
        qty: Number(item.qty),
      });
    }
    state.items = [...byProduct.values()];

    $('scan-supplier').textContent = state.supplierName;
    renderItems();
    setFeedback('idle', 'Folytathatod', null,
      `${state.items.length} tétel már be van olvasva.`);
    show('scan');
  } catch (err) {
    $('start-error').textContent = err.message;
  }
}

async function startReceipt() {
  const supplierId = Number($('supplier').value);
  if (!supplierId) return;

  $('start-error').textContent = '';
  try {
    const receipt = await api('/receipts', {
      method: 'POST',
      body: { supplier_id: supplierId },
    });
    state.receiptId = receipt.id;
    state.supplierName = receipt.supplier_name || '';
    state.items = [];
    state.lastProductCode = null;
    state.lastProductId = null;
    state.quantityBase = 0;
    state.step = 'product';

    $('scan-supplier').textContent = state.supplierName;
    renderItems();
    setFeedback('idle', 'Olvasd be az első terméket', null, 'A szkenner készen áll.');
    show('scan');
  } catch (err) {
    $('start-error').textContent = err.message;
  }
}

/* --- visszajelző mező --------------------------------------------------- */

function setFeedback(stateName, name, qty, hint, unit = 'db') {
  const box = $('feedback');
  box.dataset.state = stateName;
  $('fb-name').textContent = name;
  $('fb-hint').textContent = hint || '';

  const row = $('fb-row');
  if (qty === null || qty === undefined) {
    row.hidden = true;
  } else {
    row.hidden = false;
    $('fb-qty').textContent = formatQty(qty);
    $('fb-unit').textContent = unit;
  }
}

/* A felismert szöveg kiírása. Rövid, de mindig ott van, hogy látszódjon,
   miből dolgozott a rendszer. */
function showHeard(text) {
  const el = $('fb-heard');
  if (!el) return;
  el.textContent = text ? `„${text}”` : '';
}

function formatQty(value) {
  const n = Number(value);
  return Number.isInteger(n) ? String(n) : String(n).replace('.', ',');
}

function renderItems() {
  const list = $('item-list');
  $('scan-count').textContent = state.items.reduce((sum, i) => sum + Number(i.qty), 0);

  if (!state.items.length) {
    list.innerHTML = '<p class="empty">Még nincs beolvasott tétel.</p>';
    return;
  }

  list.innerHTML = '';
  // legutóbbi elöl — a raktáros azt akarja ellenőrizni
  [...state.items].reverse().forEach((item, index) => {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'row' + (index === 0 ? ' row--fresh' : '');
    row.innerHTML =
      `<span class="row__name"></span>` +
      `<span class="row__qty"></span>` +
      `<span class="row__unit"></span>` +
      `<span class="row__chevron" aria-hidden="true">›</span>`;
    row.querySelector('.row__name').textContent = item.name;
    row.querySelector('.row__qty').textContent = formatQty(item.qty);
    row.querySelector('.row__unit').textContent = item.unit;
    row.addEventListener('click', () => openEditSheet(item));
    list.appendChild(row);
  });
}

function upsertItem(productId, name, unit, totalQty) {
  const existing = state.items.find((i) => i.productId === productId);
  if (existing) {
    existing.qty = totalQty;
    // a frissített tétel kerüljön a lista végére (= megjelenítésben elöl)
    state.items = state.items.filter((i) => i.productId !== productId);
    state.items.push(existing);
  } else {
    state.items.push({ productId, name, unit, qty: totalQty });
  }
  renderItems();
}

/* --- beolvasás feldolgozása --------------------------------------------- */

async function sendScan(code, qty = 1) {
  try {
    const res = await api(`/receipts/${state.receiptId}/scan`, {
      method: 'POST',
      body: { code, qty },
    });

    if (res.status === 'unknown') {
      state.lastProductCode = null;
      state.lastProductId = null;
      setFeedback('stop', res.message, null, `Vonalkód: ${code}`);
      beep('stop');
      say('Tedd félre, ismeretlen termék');
      return;
    }

    if (res.status === 'inactive') {
      state.lastProductCode = null;
      setFeedback('warn', res.message, null, '');
      beep('warn');
      say('Tedd félre, inaktív termék');
      return;
    }

    state.lastProductCode = code;
    state.step = 'quantity';          // megvan a termék, jöhet a darabszám
    // A most beolvasott mennyiség előtti állapot: a bemondott darabszám
    // ezt fogja kiegészíteni.
    state.quantityBase = Number(res.total_qty) - Number(qty);
    setFeedback('ok', res.product_name, res.total_qty,
      'Mondd a darabszámot, vagy olvasd a következőt.', res.unit);
    beep('ok');
    say('Oké');

    // A termék azonosítója a listakezeléshez: a válasz item_ids-jei egy
    // termékhez tartoznak, ezért a kódot használjuk kulcsként.
    upsertItem(code, res.product_name, res.unit, res.total_qty);
  } catch (err) {
    setFeedback('stop', 'Nem sikerült rögzíteni', null, err.message);
    beep('stop');
  }
}

/* A termék PONTOS mennyiségének beállítása. Növelni és csökkenteni is
   lehet vele — a backend eldobja a régi sorokat és újraallokál. */
async function setQuantity(code, target) {
  try {
    const res = await api(`/receipts/${state.receiptId}/set-quantity`, {
      method: 'POST',
      body: { code, qty: target },
    });
    state.lastProductCode = code;
    state.step = 'product';           // kész a darabszám, jöhet a következő termék
    setFeedback('ok', res.product_name, res.total_qty,
      'Jöhet a következő termék.', res.unit);
    beep('ok');
    say(`Beírva ${formatQty(res.total_qty)}`);
    upsertItem(code, res.product_name, res.unit, res.total_qty);
  } catch (err) {
    setFeedback('stop', 'Nem sikerült módosítani', null, err.message);
    beep('stop');
  }
}

/* Mennyiség bemondása a legutóbb beolvasott termékhez.

   A bemondott szám a MOSTANI beolvasásra vonatkozik. Ha ugyanaz a termék
   már szerepelt a bevételezésben, a korábbi darabszám megmarad, és a
   bemondott mennyiség hozzáadódik. */
async function setQuantityForLast(spoken) {
  if (!state.lastProductCode) {
    setFeedback('warn', 'Előbb válassz terméket', null,
      'A darabszám mindig az utoljára rögzített tételre vonatkozik.');
    say('Előbb válassz terméket');
    return;
  }
  await setQuantity(state.lastProductCode, state.quantityBase + spoken);
}

/* --- tétel javítása és visszavonása ------------------------------------- */

let editing = null;   // { productId, name, unit, qty }

function openEditSheet(item) {
  editing = item;
  $('edit-name').textContent = item.name;
  $('edit-qty').value = formatQty(item.qty);
  $('edit-sheet').hidden = false;
  setTimeout(() => $('edit-qty').select(), 60);
}

function closeEditSheet() {
  editing = null;
  $('edit-sheet').hidden = true;
  focusScanner();
}

function nudgeQty(delta) {
  const field = $('edit-qty');
  const next = Math.max(0, (Number(field.value.replace(',', '.')) || 0) + delta);
  field.value = formatQty(next);
}

async function saveEdit() {
  if (!editing) return;
  const target = Number($('edit-qty').value.replace(',', '.'));
  if (!Number.isFinite(target) || target < 0) {
    return;
  }
  const code = editing.productId;
  closeEditSheet();
  if (target === 0) {
    await removeProduct(code);
  } else {
    // A javító lapon a TELJES darabszám látszik, ezért itt abszolút érték.
    state.quantityBase = 0;
    state.lastProductCode = code;
    await setQuantity(code, target);
  }
}

async function removeProduct(code) {
  try {
    const res = await api(`/receipts/${state.receiptId}/products/${encodeURIComponent(code)}`, {
      method: 'DELETE',
    });
    state.items = state.items.filter((i) => i.productId !== code);
    if (state.lastProductCode === code) {
      state.lastProductCode = null;
      state.quantityBase = 0;
    }
    state.step = 'product';
    renderItems();
    setFeedback('idle', 'Tétel visszavonva', null, res.product_name);
    say('Visszavonva');
  } catch (err) {
    setFeedback('stop', 'Nem sikerült visszavonni', null, err.message);
    beep('stop');
  }
}

/* --- Bluetooth vonalkódolvasó ------------------------------------------- */

/* A szkenner HID módban billentyűzetként küld, de US kiosztás szerint:
   magyar kiosztáson a '0' helyett 'ö' érkezne. Ezért nem a mező értékére
   támaszkodunk, hanem a fizikai billentyűkódra (event.code), ami
   kiosztásfüggetlen. Ugyanez javítja a magyar QWERTZ y/z cserét is. */

const DIGIT_CODES = {
  Digit0: '0', Digit1: '1', Digit2: '2', Digit3: '3', Digit4: '4',
  Digit5: '5', Digit6: '6', Digit7: '7', Digit8: '8', Digit9: '9',
  Numpad0: '0', Numpad1: '1', Numpad2: '2', Numpad3: '3', Numpad4: '4',
  Numpad5: '5', Numpad6: '6', Numpad7: '7', Numpad8: '8', Numpad9: '9',
};

let scanBuffer = '';
let scanTimer = null;

function focusScanner() {
  const el = $('scan-catcher');
  if (el && document.activeElement !== el) el.focus({ preventScroll: true });
}

function handleScanKey(event) {
  // A javító lap nyitva: a beírás a mennyiség mezőé.
  if (!$('edit-sheet').hidden) return;

  // Ha a felhasználó épp beír valamit egy valódi mezőbe, nem nyúlunk hozzá.
  const tag = document.activeElement?.tagName;
  const inRealField =
    (tag === 'INPUT' && document.activeElement.id !== 'scan-catcher') ||
    tag === 'SELECT' || tag === 'TEXTAREA';
  if (inRealField) return;

  if (event.code === 'Enter' || event.code === 'NumpadEnter') {
    event.preventDefault();
    const code = scanBuffer;
    scanBuffer = '';
    if (code.length >= 4) sendScan(code, 1);
    return;
  }

  let char = DIGIT_CODES[event.code];
  if (!char && /^Key[A-Z]$/.test(event.code)) char = event.code.slice(3);
  if (!char && event.code === 'Minus') char = '-';
  if (!char) return;

  event.preventDefault();
  scanBuffer += char;

  // Ha a szkenner nem küld Entert, egy rövid szünet után magunk zárjuk le.
  clearTimeout(scanTimer);
  scanTimer = setTimeout(() => {
    const code = scanBuffer;
    scanBuffer = '';
    if (code.length >= 8) sendScan(code, 1);
  }, 120);
}

/* --- képernyő ébren tartása ---------------------------------------------

   Ha a képernyő elalszik, a rendszer a mikrofont is felfüggeszti, és a
   hangvezérlés megszakad. A böngésző nem tudja külön ébren tartani a
   mikrofont, ezért a képernyőt tartjuk ébren, amíg tart a bevételezés.
   ------------------------------------------------------------------------ */

let wakeLock = null;

async function keepScreenAwake() {
  if (!('wakeLock' in navigator) || wakeLock) return;
  try {
    wakeLock = await navigator.wakeLock.request('screen');
    // A rendszer elveheti (pl. háttérbe kerül az app) — ilyenkor
    // a visszatéréskor újra kérjük.
    wakeLock.addEventListener('release', () => { wakeLock = null; });
  } catch (_) {
    // Nem támogatott vagy megtagadva: az app ettől még működik,
    // csak a képernyő elalhat.
    wakeLock = null;
  }
}

async function releaseScreenLock() {
  if (!wakeLock) return;
  try { await wakeLock.release(); } catch (_) { /* már elengedve */ }
  wakeLock = null;
}

// Ha az app visszatér az előtérbe, a zárolás és a mikrofon is újraindul.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible') return;
  if ($('screen-scan').classList.contains('is-active')) {
    keepScreenAwake();
    if (voice.enabled) voice.launch();
    focusScanner();
  }
});

/* --- beszélt visszajelzés ------------------------------------------------

   A raktáros ne kényszerüljön a képernyőt figyelni: minden lényeges
   eseményt kimondunk. Beszéd közben a felismerést szüneteltetjük, hogy
   az app ne hallja vissza saját magát.
   ------------------------------------------------------------------------ */

const synth = window.speechSynthesis;
let huVoice = null;

function pickVoice() {
  if (!synth) return;
  const voices = synth.getVoices();
  huVoice = voices.find((v) => v.lang && v.lang.toLowerCase().startsWith('hu')) || null;
}

if (synth) {
  pickVoice();
  synth.addEventListener?.('voiceschanged', pickVoice);
}

let speakWatchdog = null;

function say(text) {
  if (!synth || !text) return;
  try {
    synth.cancel();                 // a régi mondat ne torlódjon
    voice.mute();                   // ne hallja vissza magát

    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = 'hu-HU';
    if (huVoice) utterance.voice = huVoice;
    utterance.rate = 1.15;          // gyors, de érthető
    utterance.onend = () => finishSpeaking();
    utterance.onerror = () => finishSpeaking();
    synth.speak(utterance);

    /* Androidon az onend néha egyszerűen nem jön meg — ilyenkor a
       mikrofon némán maradna, és a raktáros hiába mondaná a darabszámot.
       Ezért figyeljük, mikor hallgat el ténylegesen, és van egy felső
       időkorlát is. */
    clearInterval(speakWatchdog);
    const startedAt = Date.now();
    const maxWait = 1500 + text.length * 90;
    speakWatchdog = setInterval(() => {
      if (!synth.speaking || Date.now() - startedAt > maxWait) finishSpeaking();
    }, 250);
  } catch (_) {
    finishSpeaking();
  }
}

function finishSpeaking() {
  clearInterval(speakWatchdog);
  speakWatchdog = null;
  voice.unmute();
}

/* --- hang --------------------------------------------------------------- 

   Kihangosított mód: a mikrofon nyitva marad, a raktáros nem nyúl a
   telefonhoz. A felismerés minden csendszünet után magától újraindul.
   ------------------------------------------------------------------------ */

const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;

const NUMBER_WORDS = {
  egy: 1, kettő: 2, ketto: 2, két: 2, ket: 2, három: 3, harom: 3, négy: 4, negy: 4,
  öt: 5, ot: 5, hat: 6, hét: 7, het: 7, nyolc: 8, kilenc: 9, tíz: 10, tiz: 10,
  tizenegy: 11, tizenkettő: 12, tizenketto: 12, tizenkét: 12, tizenhárom: 13,
  tizenharom: 13, tizennégy: 14, tizennegy: 14, tizenöt: 15, tizenot: 15,
  tizenhat: 16, tizenhét: 17, tizenhet: 17, tizennyolc: 18, tizenkilenc: 19,
  húsz: 20, husz: 20, harminc: 30, negyven: 40, ötven: 50, otven: 50,
  hatvan: 60, hetven: 70, nyolcvan: 80, kilencven: 90, száz: 100, szaz: 100,
};

/* A magyar 20-99 alakjai külön tőből képződnek: huszon-, harminc-, ... */
const TENS_PREFIX = {
  huszon: 20, harminc: 30, negyven: 40, ötven: 50, otven: 50,
  hatvan: 60, hetven: 70, nyolcvan: 80, kilencven: 90,
};

/* Számfelismerés SZIGORÚAN: csak akkor szám, ha a TELJES elhangzott
   mondat az. Enélkül a „kcf 250” mennyiségnek számítana, holott
   terméknév — pont ez keverte össze korábban a két üzemmódot.

   A „darab” utótag megengedett: „öt darab”, „12 db”. */
function parseHungarianNumber(text) {
  let clean = text.toLowerCase().trim().replace(/[.,!?]/g, '');

  // a mértékegység elhagyható: 'öt darab' -> 'öt'
  clean = clean.replace(/\s*(darab|darabot|db)$/, '').trim();
  if (!clean) return null;

  // csak számjegyek: '25'
  if (/^\d+$/.test(clean)) return Number(clean);

  // egyetlen számnév: 'öt', 'huszonöt'
  const single = wordToNumber(clean);
  if (single !== null) return single;

  // külön ejtett alak: 'harminc kettő'
  const parts = clean.split(/\s+/);
  let total = 0;
  for (const part of parts) {
    const value = wordToNumber(part);
    if (value === null) return null;   // egyetlen nem-szám szó -> nem mennyiség
    total += value;
  }
  return parts.length ? total : null;
}

function wordToNumber(word) {
  if (/^\d+$/.test(word)) return Number(word);
  if (NUMBER_WORDS[word] !== undefined) return NUMBER_WORDS[word];

  // százas alakok: 'százhúsz', 'kétszázötven'
  const hundreds = word.match(/^(két|ket|három|harom|négy|negy|öt|ot|hat|hét|het|nyolc|kilenc)?(száz|szaz)(.*)$/);
  if (hundreds) {
    const multiplier = hundreds[1] ? NUMBER_WORDS[hundreds[1]] : 1;
    const rest = hundreds[3];
    if (!rest) return multiplier * 100;
    const tail = wordToNumber(rest);
    if (tail !== null) return multiplier * 100 + tail;
    return null;
  }

  // összetett alak egy szóban: 'huszonöt', 'harminckettő'
  for (const [prefix, base] of Object.entries(TENS_PREFIX)) {
    if (word.startsWith(prefix) && word.length > prefix.length) {
      const rest = word.slice(prefix.length);
      if (NUMBER_WORDS[rest] !== undefined) return base + NUMBER_WORDS[rest];
    }
  }
  return null;
}

const ORDINALS = {
  első: 0, elso: 0, egyes: 0,
  második: 1, masodik: 1, kettes: 1,
  harmadik: 2, hármas: 2, harmas: 2,
  negyedik: 3, négyes: 3, negyes: 3,
  ötödik: 4, otodik: 4, ötös: 4, otos: 4,
};

function parseOrdinal(text) {
  const clean = text.toLowerCase().trim().replace(/[.,!?]/g, '');
  return ORDINALS[clean] !== undefined ? ORDINALS[clean] : null;
}

/* Kihangosított figyelés: egy példány, ami a képernyőnek megfelelően
   más-más módban dolgozza fel a hallottakat. */
const voice = {
  recognition: null,
  enabled: false,
  muted: false,       // amíg az app beszél
  running: false,     // fut-e ténylegesen a felismerés
  mode: 'scan',       // 'scan' vagy 'search'
  restarting: false,
  keepAlive: null,

  supported() { return Boolean(SpeechRecognition); },

  start(mode) {
    if (!this.supported()) {
      setFeedback('warn', 'A hangfelismerés nem érhető el', null,
        'Ez a böngésző nem támogatja. Használd a szkennert vagy a keresést.');
      return;
    }
    this.mode = mode;
    this.enabled = true;
    this.muted = false;
    this.launch();
    this.startKeepAlive();
    updateVoiceButtons();
    if (mode === 'scan') {
      $('fb-hint').textContent = state.step === 'quantity'
        ? 'Mondd a darabszámot.'
        : 'Mondd a termék nevét, vagy olvasd be a vonalkódot.';
    }
  },

  stop() {
    if (this.enabled && this.mode === 'scan') {
      $('fb-hint').textContent = 'A szkenner készen áll.';
    }
    this.enabled = false;
    this.running = false;
    clearInterval(this.keepAlive);
    this.keepAlive = null;
    if (this.recognition) {
      try { this.recognition.abort(); } catch (_) { /* már leállt */ }
    }
    updateVoiceButtons();
  },

  /* Őrjárat: ha a felismerés bármi miatt leállt (a böngésző elengedte,
     a beszéd után nem indult vissza, hálózati hiba), magától újraindul.
     Enélkül a mikrofon csendben kikapcsolna, és a raktáros hiába
     beszélne tovább. */
  startKeepAlive() {
    clearInterval(this.keepAlive);
    this.keepAlive = setInterval(() => {
      if (this.enabled && !this.muted && !this.running) this.launch();
    }, 1200);
  },

  /* Beszéd alatt leállítjuk a felismerést, utána visszakapcsoljuk. */
  mute() {
    if (!this.enabled) return;
    this.muted = true;
    this.running = false;
    if (this.recognition) {
      try { this.recognition.abort(); } catch (_) { /* már leállt */ }
    }
  },

  unmute() {
    this.muted = false;
    if (!this.enabled) return;
    setTimeout(() => { if (this.enabled && !this.muted) this.launch(); }, 200);
  },

  launch() {
    if (this.muted || this.running) return;
    const recognition = new SpeechRecognition();
    recognition.lang = 'hu-HU';
    recognition.continuous = true;
    recognition.interimResults = false;
    recognition.maxAlternatives = 3;

    recognition.onstart = () => { this.running = true; };

    recognition.onresult = (event) => {
      for (let i = event.resultIndex; i < event.results.length; i++) {
        if (!event.results[i].isFinal) continue;
        const alternatives = [];
        for (let j = 0; j < event.results[i].length; j++) {
          alternatives.push(event.results[i][j].transcript);
        }
        this.handle(alternatives);
      }
    };

    recognition.onerror = (event) => {
      this.running = false;
      // 'no-speech' és 'aborted' természetes szünet — az onend újraindítja
      if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
        this.enabled = false;
        updateVoiceButtons();
        setFeedback('warn', 'A mikrofon nincs engedélyezve', null,
          'Engedélyezd a böngészőben, vagy használd a gombokat.');
      }
    };

    recognition.onend = () => {
      this.running = false;
      // A Chrome hosszabb csend után magától leáll — újraindítjuk.
      // (Ha nem sikerül, az őrjárat úgyis megpróbálja.)
      if (!this.enabled || this.restarting || this.muted) return;
      this.restarting = true;
      setTimeout(() => {
        this.restarting = false;
        this.launch();
      }, 250);
    };

    this.recognition = recognition;
    // Optimista jelzés: az onstart késhet, és addig az őrjárat egy
    // második példányt indítana ugyanarra a mikrofonra.
    this.running = true;
    try {
      recognition.start();
    } catch (_) {
      // 'InvalidStateError': az előző példány még nem állt le teljesen.
      // Nem baj — az őrjárat rövidesen újrapróbálja.
      this.running = false;
    }
  },

  handle(alternatives) {
    if (this.mode === 'search') {
      $('search-input').value = alternatives[0];
      runSearch(alternatives[0]);
      return;
    }

    // Mindig látszódjon, mit hallott — enélkül a hibát nem lehet megfogni.
    showHeard(alternatives[0]);

    // a javító lap nyitva van -> nem nyúlunk bele
    if (!$('edit-sheet').hidden) return;

    // a találati listán a sorszám kiválaszt: „első”, „második”, ...
    if ($('screen-search').classList.contains('is-active')) {
      const index = parseOrdinal(alternatives[0]);
      const results = document.querySelectorAll('#search-results .result');
      if (index !== null && results[index]) {
        results[index].click();
        return;
      }
      // egyébként új keresés a hallottakra
      const term = alternatives[0].trim();
      if (term.length >= 3) {
        $('search-input').value = term;
        runSearch(term);
      }
      return;
    }

    if (!$('screen-scan').classList.contains('is-active')) return;

    // Visszavonás bármelyik lépésben működik.
    const first = alternatives[0].toLowerCase().trim().replace(/[.,!?]/g, '');
    if (/^(töröl|torol|vissza|visszavon|visszavonás|visszavonas)$/.test(first)) {
      if (state.lastProductCode) removeProduct(state.lastProductCode);
      else say('Nincs mit visszavonni');
      return;
    }

    // Kényszerítő parancsok, ha a felismerés félreértené a szándékot:
    //   'keresd ...' / 'termék ...'  -> mindig keresés
    //   'darab ...'                  -> mindig mennyiség
    const forcedSearch = first.match(/^(keresd|keres|keresés|termék|termek)\s+(.+)$/);
    if (forcedSearch) {
      state.step = 'product';
      voiceSearch(forcedSearch[2]);
      return;
    }

    const forcedQty = first.match(/^(darab|mennyiség|mennyiseg)\s+(.+)$/);
    if (forcedQty) {
      const value = parseHungarianNumber(forcedQty[2]);
      if (value !== null && value > 0) { setQuantityForLast(value); return; }
    }

    if (state.step === 'quantity') {
      for (const text of alternatives) {
        const value = parseHungarianNumber(text);
        if (value !== null && value > 0 && value < 10000) {
          setQuantityForLast(value);
          return;
        }
      }
      // Nem szám: a raktáros továbblépett a következő termékre, a
      // mostani marad 1 darab. Keresésként értelmezzük.
      state.step = 'product';
    }

    // Termékkeresés. A keresőszó tartalmazhat számot ('kcf 250') — itt
    // nem kell szétválasztani, mert most nem mennyiséget várunk.
    const term = alternatives[0].trim();
    if (term.length >= 2) voiceSearch(term);
  },
};

/* Hangos termékkeresés a szkennelő képernyőről.
   Egy találat: azonnal rögzítjük. Több: kiírjuk a listát választásra. */
async function voiceSearch(term) {
  setFeedback('idle', `Keresem: ${term}`, null, '');
  try {
    const results = await api(`/products/search?q=${encodeURIComponent(term)}&limit=25`);

    if (!results.length) {
      state.step = 'product';
      setFeedback('warn', 'Nincs találat', null,
        `Ezt hallottam: „${term}”. Mondd újra, vagy máshogy.`);
      say('Nincs találat, mondd újra');
      return;
    }

    if (results.length === 1) {
      await sendScan(results[0].ean || results[0].sku, 1);
      return;
    }

    renderSearchResults(results);
    $('search-input').value = term;
    show('search');
    say('Válassz a listából');
  } catch (err) {
    setFeedback('stop', 'A keresés nem sikerült', null, err.message);
  }
}

function updateVoiceButtons() {
  const scanBtn = $('voice-btn');
  const searchBtn = $('search-voice');
  const onScan = voice.enabled && voice.mode === 'scan';
  const onSearch = voice.enabled && voice.mode === 'search';

  scanBtn.classList.toggle('btn--listening', onScan);
  scanBtn.textContent = onScan ? 'Hang be' : 'Hang';
  scanBtn.setAttribute('aria-pressed', String(onScan));

  searchBtn.classList.toggle('btn--listening', onSearch || onScan);
  searchBtn.textContent = (onSearch || onScan) ? 'Hallgat' : 'Hang';
  searchBtn.setAttribute('aria-pressed', String(onSearch));
}

/* --- keresés ------------------------------------------------------------ */

let searchTimer = null;

function renderSearchResults(results) {
  const list = $('search-results');
  list.innerHTML = '';
  results.forEach((p, index) => {
    const btn = document.createElement('button');
    btn.className = 'result';
    // A sorszám nem dísz: hanggal erre lehet hivatkozni („második”).
    btn.innerHTML =
      '<span class="result__no"></span>' +
      '<span class="result__text"><span class="result__name"></span>' +
      '<span class="result__sku"></span></span>';
    btn.querySelector('.result__no').textContent = index + 1;
    btn.querySelector('.result__name').textContent = p.name;
    btn.querySelector('.result__sku').textContent = p.sku;
    btn.addEventListener('click', () => {
      if (voice.enabled) voice.start('scan');
      show('scan');
      sendScan(p.ean || p.sku, 1);
    });
    list.appendChild(btn);
  });
}

async function runSearch(term) {
  const list = $('search-results');
  if (!term || term.length < 2) {
    list.innerHTML = '<p class="empty">Írd be a termék nevének egy részletét.</p>';
    return;
  }

  try {
    const results = await api(`/products/search?q=${encodeURIComponent(term)}&limit=25`);
    if (!results.length) {
      list.innerHTML = '<p class="empty">Nincs találat. Próbálj más szót.</p>';
      return;
    }
    renderSearchResults(results);
  } catch (err) {
    list.innerHTML = '';
    const p = document.createElement('p');
    p.className = 'empty';
    p.textContent = err.message;
    list.appendChild(p);
  }
}

/* --- lezárás ------------------------------------------------------------ */

async function finishReceipt() {
  if (!state.items.length) {
    setFeedback('warn', 'Még nincs beolvasott tétel', null, 'Olvass be legalább egy terméket.');
    return;
  }
  try {
    const receipt = await api(`/receipts/${state.receiptId}/finish`, { method: 'POST' });
    voice.stop();
    const pieces = state.items.reduce((sum, i) => sum + Number(i.qty), 0);
    say('Bevételezés lezárva');
    $('done-summary').textContent =
      `${receipt.item_count} tétel, ${formatQty(pieces)} darab rögzítve.` +
      (receipt.unknown_count ? ` ${receipt.unknown_count} termék félretéve.` : '');
    show('done');
  } catch (err) {
    setFeedback('stop', 'Nem sikerült lezárni', null, err.message);
  }
}

/* --- hangjelzés --------------------------------------------------------- */

let audioContext = null;

function beep(kind) {
  try {
    audioContext = audioContext || new (window.AudioContext || window.webkitAudioContext)();
    const osc = audioContext.createOscillator();
    const gain = audioContext.createGain();
    osc.connect(gain);
    gain.connect(audioContext.destination);

    // rövid, magas = rendben; mély, hosszabb = félretenni
    const tone = kind === 'ok' ? 1180 : kind === 'warn' ? 720 : 320;
    const length = kind === 'ok' ? 0.07 : 0.22;

    osc.frequency.value = tone;
    gain.gain.setValueAtTime(0.18, audioContext.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, audioContext.currentTime + length);
    osc.start();
    osc.stop(audioContext.currentTime + length);

    if (kind !== 'ok' && navigator.vibrate) navigator.vibrate(kind === 'stop' ? [80, 60, 80] : 60);
  } catch (_) { /* hang nélkül is működik */ }
}

/* --- események ---------------------------------------------------------- */

$('login-btn').addEventListener('click', login);
$('login-pass').addEventListener('keydown', (e) => { if (e.key === 'Enter') login(); });
$('logout-btn').addEventListener('click', logout);
$('start-btn').addEventListener('click', startReceipt);
$('finish-btn').addEventListener('click', finishReceipt);
$('voice-btn').addEventListener('click', () => {
  if (voice.enabled && voice.mode === 'scan') {
    voice.stop();
  } else {
    voice.start('scan');
    say('Hallgatlak');
  }
});
$('done-btn').addEventListener('click', openStart);

$('search-btn').addEventListener('click', () => {
  $('search-input').value = '';
  $('search-results').innerHTML = '<p class="empty">Írd be a termék nevének egy részletét.</p>';
  show('search');
});

$('search-back').addEventListener('click', () => {
  if (voice.enabled) voice.start('scan');
  show('scan');
});

$('search-input').addEventListener('input', (e) => {
  clearTimeout(searchTimer);
  const term = e.target.value.trim();
  searchTimer = setTimeout(() => runSearch(term), 250);
});

$('search-voice').addEventListener('click', () => {
  if (voice.enabled && voice.mode === 'search') voice.stop();
  else voice.start('search');
});

$('edit-plus').addEventListener('click', () => nudgeQty(1));
$('edit-minus').addEventListener('click', () => nudgeQty(-1));
$('edit-save').addEventListener('click', saveEdit);
$('edit-cancel').addEventListener('click', closeEditSheet);
$('edit-remove').addEventListener('click', () => {
  if (!editing) return;
  const code = editing.productId;
  closeEditSheet();
  removeProduct(code);
});

$('edit-sheet').addEventListener('click', (e) => {
  if (e.target === $('edit-sheet')) closeEditSheet();   // háttérre koppintva bezár
});

$('edit-qty').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); saveEdit(); }
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !$('edit-sheet').hidden) closeEditSheet();
});

document.addEventListener('keydown', handleScanKey);

// A szkenner csak akkor tud „gépelni”, ha a rejtett mező fókuszban van.
document.addEventListener('click', () => {
  if ($('screen-scan').classList.contains('is-active') && $('edit-sheet').hidden) {
    focusScanner();
  }
});

window.addEventListener('focus', () => {
  if ($('screen-scan').classList.contains('is-active')) focusScanner();
});

/* --- indulás ------------------------------------------------------------ */

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('sw.js').catch(() => { /* offline mód nélkül is megy */ });
}

if (state.token) {
  openStart().catch(() => show('login'));
} else {
  show('login');
}
