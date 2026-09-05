# Bevételező rendszer — backend terv

## Technikai stack

| Réteg | Választás | Indoklás |
|---|---|---|
| Web framework | FastAPI | Automatikus API dokumentáció, gyors, Pydantic validáció |
| ORM | SQLAlchemy 2.x | Kiforrott, jól illeszkedik a Postgres-hez |
| Migráció | Alembic | Séma-verziózás, visszagörgethető |
| Excel olvasás | pandas + xlrd | A Naturasoft `.xls` (legacy) formátumot ad |
| Excel írás | openpyxl | `.xlsx` kimenet a Naturasoft importhoz |
| Auth | JWT vagy szerver-oldali session | 4-5 fő, egyszerű username/password |
| Hosztolás | Railway | Backend + Postgres egy projektben |

> **Fontos**: a Naturasoft exportja legacy `.xls`, amit az `openpyxl` nem
> olvas. Ehhez `xlrd >= 2.0.1` kell, `pd.read_excel(..., engine="xlrd")`.

---

## Könyvtárszerkezet

```
src/
  main.py                  # FastAPI app, router regisztráció
  config.py                # környezeti változók
  db/
    session.py             # engine, session factory
    models.py              # SQLAlchemy modellek
    repositories/
      product.py
      purchase_order.py
      receipt.py
  routes/
    auth.py
    products.py            # terméktörzs import, keresés
    suppliers.py
    orders.py              # rendelés feltöltés, listázás, admin
    receipts.py            # bevételezés (raktáros + admin)
    export.py              # Naturasoft Excel generálás
  services/
    product_import.py      # terméktörzs .xls -> DB
    order_import.py        # rendelés .xls -> DB
    fifo.py                # FIFO allokációs logika
    naturasoft_export.py   # bevételezés -> .xlsx
  schemas/                 # Pydantic modellek
alembic/
```

---

## A FIFO allokáció

Ez a rendszer szíve. Amikor a raktáros beolvas egy EAN-t és megad egy
mennyiséget, a következő történik:

1. **Termék feloldás** — EAN alapján a `product` táblában.
   Ha nincs találat → `unknown_scan` sor, és a felület kiírja:
   *„Tedd félre, fel kell venni a terméket!"*. A tétel nem kerül be.

2. **Nyitott rendeléstételek lekérése** — az adott szállító `open` és
   `partial` státuszú rendeléseiből, ahol `product_id` egyezik és
   `remaining_qty > 0`, **`order_date`, majd `order_number` szerint
   rendezve**.

3. **Szétosztás** — a mennyiséget sorban felosztjuk a rendeléstételek
   maradéka között. Minden érintett rendeléstételhez külön
   `receipt_item` sor keletkezik `source = 'from_order'` értékkel,
   a `net_unit_price` a rendeléstételről öröklődik.

4. **Túlcsordulás** — ha a beolvasott mennyiség több, mint az összes
   nyitott maradék (vagy egyáltalán nincs nyitott rendelés a termékre),
   a fennmaradó rész egy `source = 'outside_order'` sorba kerül,
   `net_unit_price = NULL`. Ez nem hiba, csak jelzés az adminnak.

> A raktáros ebből semmit nem lát és nem is dönt. A képernyőn csak a
> termék neve és a beolvasott mennyiség jelenik meg.

### Fontos: a maradék csak exportkor csökken

A `purchase_order_item.received_qty` **kizárólag** az Excel generálásakor
frissül. Amíg a bevételezés `in_progress` vagy `scanned` állapotban van,
a rendelés maradéka érintetlen. Ez azt jelenti:

- Ha az admin töröl egy tételt export előtt, a mennyiség automatikusan
  visszakerül a rendelés maradékába (mert soha nem is került le róla).
- A FIFO allokáció export előtt **újraszámolható** — érdemes is
  újraszámolni exportkor, mert időközben más bevételezés is exportálódhatott.

---

## API végpontok

### Raktáros felület

```
POST   /api/receipts                      új bevételezés (supplier_id)
GET    /api/receipts/{id}                 aktuális állapot
POST   /api/receipts/{id}/scan            { code }  -> feloldás + 1 db
PATCH  /api/receipts/{id}/items/{item_id} { qty }   -> mennyiség módosítás
POST   /api/receipts/{id}/finish          in_progress -> scanned
GET    /api/products/search?q=            hangos/kézi kereséshez
```

A `/scan` végpont válasza vezérli a felületet:

```json
{
  "status": "ok",              // ok | unknown | inactive
  "product": { "name": "...", "unit": "db" },
  "receipt_item_id": 123,
  "qty": 1,
  "message": null
}
```

`status: "unknown"` esetén a `message` a *„Tedd félre…"* szöveg.

### Admin felület

```
GET    /api/orders                        lista + előrehaladás
GET    /api/orders/{id}                   tételek, bevételezés-bontással
POST   /api/orders/upload                 .xls + dátum + szállító
PATCH  /api/orders/{id}                   dátum, szállító, megjegyzés
POST   /api/orders/{id}/close             kézi lezárás
DELETE /api/orders/{id}                   csak ha nincs rá exportált bevételezés

GET    /api/receipts                      lista, szűrés státuszra
PATCH  /api/receipts/{id}/items/{item_id} javítás (csak scanned állapotban)
DELETE /api/receipts/{id}/items/{item_id} tétel törlés
POST   /api/receipts/{id}/export          -> .xlsx + status: exported
GET    /api/receipts/{id}/export/download  újraletöltés

POST   /api/products/import               terméktörzs .xls
GET    /api/suppliers  /  POST  /  PATCH
```

---

## Naturasoft export formátum

A Naturasoft „Sorok beemelése Excel fájl alapján" varázslója
oszlop-hozzárendelést kér, tehát a szerkezetet mi határozzuk meg.
**Fix sorrend**, hogy a leképezést egyszer kelljen beállítani:

| Oszlop | Tartalom | Naturasoft mező |
|---|---|---|
| A | Cikkszám | Cikkszám* (kötelező) |
| B | Megnevezés | Megnevezés |
| C | Termékkód (EAN) | Termékkód |
| D | Mennyiség | Mennyiség |
| E | Nettó beszerzési ár | Beszerzési ár (Ft) — *nettó* |
| F | ÁFA kulcs neve | ÁFA kulcs neve |
| G | Megjegyzés | Megjegyzés |

Beállítások a varázslóban:
- **Termék azonosítása: cikkszám alapján**
- **Beszerzési ár: nettó**
- **Raktár neve**: legördülőből (nem oszlopból) — pl. *Szüret utca*

### Aggregálás exportkor

Ha a FIFO ugyanazt a terméket több rendelés között osztotta szét, az
Excelben **egy sorként** érdemes szerepelnie, hacsak az árak nem térnek
el. Csoportosítás: `sku` + `net_unit_price`.

### Ellenőrzendő az első futásnál

Az ÁFA kulcs neve mezőt a Naturasoft az **ÁFA-törzsben lévő névvel**
várja (pl. `27%-os ÁFA`), a terméktörzs viszont `27%` formában tárolja.
Az első exportnál ezt le kell tesztelni, és szükség esetén egy
leképező táblát kell felvenni (`vat_rate` → `vat_name`).

---

## Terméktörzs import

Forrás: `Terméknyilvántartás.xls`

| Export oszlop | DB mező | Megjegyzés |
|---|---|---|
| Sorszám | `naturasoft_id` | egyedi kulcs, ezen párosít |
| Megnevezés | `name` | |
| Gyártók | `manufacturer` | **nem** a szállító |
| Termékkód | `ean` | **TEXT-ként**, vezető nullák megőrzésével |
| Cikkszám | `sku` | ez megy a Naturasoft importba |
| Mee. | `unit` | |
| ÁFA | `vat_rate` | |
| Súly (kg) | `weight_kg` | |
| Termékcsoportok | `product_group` | |
| Törölt (inaktív) | `inactive` | inaktív termék ne jelenjen meg a keresőben |

Szabályok:
- **Frissít + hozzáad, soha nem töröl.** Egy hibás export nem üríti ki a törzset.
- Az `ean` mezőt `str()`-ként kell olvasni; a pandas float-ként hozza
  (`5413470315539.0`), amiből vezető nulla elveszik.
- Az import naplózza a warningokat: EAN nélküli termék, duplikált EAN,
  hiányzó cikkszám.

---

## Rendelés import

Forrás: `Szállítói_megrendelés__9686__tételek_listája.xls`

- **Rendelésszám**: a fájlnévből, regex `megrendelés__(\d+)__`.
  A feltöltő űrlapon megjelenik, javítható.
- **Dátum**: a felhasználó adja meg (alapból ma). Ez a FIFO alapja.
- **Szállító**: a felhasználó választja (az export nem tartalmazza).
- **Az utolsó összesítő sort ki kell szűrni** — ott üres a
  „Termék sorszám" és a „Cikkszám", csak az összegek vannak kitöltve.
- **Raktár**: a „Raktár" oszlopból, rendelés szinten.
- Ha a rendelésszám már létezik: rákérdez (felülír / elutasít).

| Export oszlop | DB mező |
|---|---|
| Termék sorszám | `naturasoft_id` → `product` párosítás |
| Megnevezés | `name_snapshot` |
| Termékkód | `ean_snapshot` |
| Cikkszám | `sku_snapshot` |
| Mennyiség | `ordered_qty` |
| Mee. | `unit` |
| Nettó egységár | `net_unit_price` |
| ÁFA% | `vat_rate` |
| Raktár | `purchase_order.warehouse` |

---

## Vonalkód-beolvasás (frontend)

A komissiózó projektben megoldott probléma, egy az egyben átemelendő:

A Bluetooth HID szkenner US kiosztás szerint küld, a magyar kiosztás
viszont a `0`-t `ö`-nek fordítja. **Nem a mező értékére támaszkodunk**,
hanem a fizikai billentyűkódra (`event.code`), ami kiosztásfüggetlen:

```js
const digitCodeMap = {
  Digit0:'0', Digit1:'1', Digit2:'2', Digit3:'3', Digit4:'4',
  Digit5:'5', Digit6:'6', Digit7:'7', Digit8:'8', Digit9:'9',
  Numpad0:'0', Numpad1:'1', Numpad2:'2', Numpad3:'3', Numpad4:'4',
  Numpad5:'5', Numpad6:'6', Numpad7:'7', Numpad8:'8', Numpad9:'9',
};
let scanBuffer = '';

scanInput.addEventListener('keydown', (e) => {
  if (e.code === 'Enter' || e.code === 'NumpadEnter') {
    e.preventDefault();
    const ean = scanBuffer;
    scanBuffer = '';
    scanInput.value = '';
    if (ean) handleScan(ean);
    return;
  }
  const digit = digitCodeMap[e.code];
  if (digit !== undefined) {
    e.preventDefault();          // a rossz karakter ne kerüljön a mezőbe
    scanBuffer += digit;
    scanInput.value = scanBuffer; // helyes visszajelzés
  }
});
```

A scan mező mindig kapja vissza a fókuszt — kivéve, ha a felhasználó
épp egy mennyiség-mezőben van.

---

## Nyitott kérdések a fejlesztés előtt

1. **ÁFA kulcs neve** — a Naturasoft pontosan milyen szöveget vár?
   Ezt az első teszt-importnál kell tisztázni.
2. **Több raktár** — van-e a Szüret utcán kívül másik? Ha igen, a
   bevételezéshez raktár-választás is kell.
3. **Hangparancsok szótára** — a mennyiségen kívül mit ismerjen fel?
   Javaslat kezdésnek: számok 1–99, „töröl", „kész".
4. **Terméktörzs mérete** — a keresés megvalósítását befolyásolja
   (néhány ezer termékig a szerver-oldali trigram keresés bőven elég).
