-- ============================================================
-- Raktári bevételező rendszer - PostgreSQL séma
-- Naturasoft integráció (Excel import/export alapon)
-- ============================================================

-- ------------------------------------------------------------
-- Felhasználók
-- 4-5 fő, mindenki mindent csinálhat. Az azonosítás célja:
-- naplózás (ki csinálta) és a bevételezés zárolása.
-- ------------------------------------------------------------
CREATE TABLE app_user (
    id              BIGSERIAL PRIMARY KEY,
    username        TEXT        NOT NULL UNIQUE,
    password_hash   TEXT        NOT NULL,
    display_name    TEXT        NOT NULL,
    active          BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------
-- Szállítók
-- A Naturasoft rendelés-export nem tartalmazza a szállítót,
-- ezért kézzel visszük fel őket, és a bevételezés indításakor
-- a raktáros választ / bemond egyet.
-- ------------------------------------------------------------
CREATE TABLE supplier (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT        NOT NULL UNIQUE,
    active          BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------
-- Terméktörzs
-- Forrás: Naturasoft "Terméknyilvántartás" export (.xls).
-- Az adminban kézzel frissíthető: az import FRISSÍT + HOZZÁAD,
-- soha nem töröl.
--
-- naturasoft_id = az export "Sorszám" oszlopa. Ez a stabil belső
-- azonosító, ami a rendelés-exportban "Termék sorszám" néven
-- szerepel -> ezen párosítunk, nem EAN-on és nem cikkszámon.
--
-- ean: TEXT, mert lehet vezető nullás, és nem minden terméknél
-- van (a mintában a termékek jelentős részénél hiányzott).
-- ------------------------------------------------------------
CREATE TYPE product_source AS ENUM ('naturasoft', 'unas', 'order');

CREATE TABLE product (
    id                  BIGSERIAL PRIMARY KEY,
    naturasoft_id       BIGINT      NOT NULL UNIQUE,     -- "Sorszám" / "Termék sorszám"
    ean                 TEXT,                            -- "Termékkód"
    sku                 TEXT        NOT NULL,            -- "Cikkszám" - ezzel azonosít a Naturasoft import
    name                TEXT        NOT NULL,            -- "Megnevezés"
    manufacturer        TEXT,                            -- "Gyártók" (NEM azonos a szállítóval)
    unit                TEXT        NOT NULL DEFAULT 'db', -- "Mee."
    vat_rate            TEXT,                            -- "ÁFA" (pl. '27%')
    vat_name            TEXT,                            -- Naturasoft ÁFA-törzs szerinti NÉV (pl. '27%-os ÁFA')
    weight_kg           NUMERIC(12,3),
    product_group       TEXT,                            -- "Termékcsoportok"
    inactive            BOOLEAN     NOT NULL DEFAULT FALSE, -- "Törölt (inaktív)"
    -- Igaz, ha a termék biztosan létezik a Naturasoftban: vagy szerepelt az
    -- induló Naturasoft importban, vagy volt már szállítói megrendelésen.
    -- Ha hamis, a bevételezés importja elutasítaná a sort -> admin figyelmeztetés.
    -- A raktáros ebből semmit nem lát.
    in_naturasoft       BOOLEAN     NOT NULL DEFAULT FALSE,
    source              product_source NOT NULL DEFAULT 'naturasoft',
    imported_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- EAN-ra gyors keresés (szkennelés). Nem UNIQUE: elvben előfordulhat
-- duplikáció a törzsben; ezt az import figyelmezteti, de nem blokkolja.
CREATE INDEX idx_product_ean ON product (ean) WHERE ean IS NOT NULL;
CREATE INDEX idx_product_sku ON product (sku);

-- Hangos / kézi kereséshez: ékezet- és kisbetű-független részszavas keresés.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE INDEX idx_product_name_trgm ON product USING gin (name gin_trgm_ops);

-- ------------------------------------------------------------
-- Szállítói megrendelés (fejléc)
--
-- order_number: a fájlnévből ("Szállítói_megrendelés__9686__...")
-- order_date:   a feltöltéskor adja meg a felhasználó. A FIFO EZT
--               használja (nem a rendelésszámot).
-- status:       open | partial | closed
--               closed lehet automatikus (minden tétel megérkezett)
--               vagy kézi (a maradék már nem fog megérkezni).
-- ------------------------------------------------------------
CREATE TYPE purchase_order_status AS ENUM ('open', 'partial', 'closed');

CREATE TABLE purchase_order (
    id                  BIGSERIAL PRIMARY KEY,
    order_number        TEXT        NOT NULL UNIQUE,     -- pl. '9686'
    order_date          DATE        NOT NULL,            -- FIFO alap
    supplier_id         BIGINT      REFERENCES supplier(id),
    warehouse           TEXT,                            -- "Raktár" oszlopból (pl. 'Szüret utca')
    status              purchase_order_status NOT NULL DEFAULT 'open',
    source_filename     TEXT,
    uploaded_by         BIGINT      REFERENCES app_user(id),
    uploaded_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at           TIMESTAMPTZ,
    closed_manually     BOOLEAN     NOT NULL DEFAULT FALSE,
    note                TEXT
);

-- FIFO sorrend: dátum, azonos dátum esetén rendelésszám.
CREATE INDEX idx_po_fifo ON purchase_order (order_date, order_number)
    WHERE status IN ('open', 'partial');

-- ------------------------------------------------------------
-- Megrendelés tételek
--
-- A snapshot mezők (sku/name/ean/ár) a feltöltéskori állapotot
-- őrzik: ha a terméktörzs később változik, a rendelés akkor is
-- azt mutatja, ami rendelve lett.
--
-- received_qty: KUMULATÍV, csak EXPORTÁLT bevételezésekből.
--               A raktáros beolvasása önmagában nem növeli.
-- ------------------------------------------------------------
CREATE TABLE purchase_order_item (
    id                  BIGSERIAL PRIMARY KEY,
    purchase_order_id   BIGINT      NOT NULL REFERENCES purchase_order(id) ON DELETE CASCADE,
    product_id          BIGINT      REFERENCES product(id),
    naturasoft_id       BIGINT      NOT NULL,            -- "Termék sorszám"
    sku_snapshot        TEXT        NOT NULL,
    ean_snapshot        TEXT,
    name_snapshot       TEXT        NOT NULL,
    unit                TEXT        NOT NULL DEFAULT 'db',
    ordered_qty         NUMERIC(12,3) NOT NULL,
    received_qty        NUMERIC(12,3) NOT NULL DEFAULT 0,
    net_unit_price      NUMERIC(14,4),                   -- "Nettó egységár"
    vat_rate            TEXT,
    line_no             INT         NOT NULL,            -- eredeti sorrend a fájlban
    CONSTRAINT uq_po_item UNIQUE (purchase_order_id, naturasoft_id)
);

CREATE INDEX idx_po_item_product ON purchase_order_item (product_id);

-- Maradék mennyiség (számított):
--   ordered_qty - received_qty
CREATE VIEW v_po_item_remaining AS
SELECT
    i.*,
    (i.ordered_qty - i.received_qty) AS remaining_qty
FROM purchase_order_item i;

-- ------------------------------------------------------------
-- Bevételezés (fejléc)
--
-- Állapotok:
--   in_progress : a raktáros dolgozik rajta (ZÁROLT: csak locked_by_id
--                 nyithatja meg)
--   scanned     : a raktáros végzett, az admin szerkesztheti
--   exported    : az Excel legenerálva -> ZÁROLT, nem szerkeszthető.
--                 Ekkor és csak ekkor nő a purchase_order_item.received_qty.
-- ------------------------------------------------------------
CREATE TYPE receipt_status AS ENUM ('in_progress', 'scanned', 'exported');

CREATE TABLE receipt (
    id                  BIGSERIAL PRIMARY KEY,
    supplier_id         BIGINT      NOT NULL REFERENCES supplier(id),
    status              receipt_status NOT NULL DEFAULT 'in_progress',
    created_by          BIGINT      NOT NULL REFERENCES app_user(id),
    locked_by           BIGINT      REFERENCES app_user(id),  -- egy user dolgozhat rajta
    delivery_note_no    TEXT,                                 -- szállítólevél száma (opcionális)
    -- Hivatkozási szám: a megrendelés száma, ami a Naturasoft bevételezés
    -- fejlécébe kerül. Automatikusan az érintett rendelésekből töltődik
    -- (több rendelésnél vesszővel elválasztva), az admin felülírhatja.
    reference_number    TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    scanned_at          TIMESTAMPTZ,
    exported_at         TIMESTAMPTZ,
    exported_by         BIGINT      REFERENCES app_user(id),
    export_filename     TEXT,
    note                TEXT
);

CREATE INDEX idx_receipt_status ON receipt (status, created_at DESC);

-- ------------------------------------------------------------
-- Bevételezés tételek
--
-- Egy beolvasott termék TÖBB sorra is eshet, ha a FIFO több
-- megrendelés között osztja szét a mennyiséget.
--
-- source:
--   from_order      : nyitott rendelésről, FIFO alapján
--   outside_order   : a terméktörzsben megvan, de nincs nyitott
--                     rendelésen (vagy túlcsordult a maradékon)
--
-- Ismeretlen EAN (törzsben sincs) NEM ide kerül -> unknown_scan.
-- ------------------------------------------------------------
CREATE TYPE receipt_item_source AS ENUM ('from_order', 'outside_order');

CREATE TABLE receipt_item (
    id                      BIGSERIAL PRIMARY KEY,
    receipt_id              BIGINT      NOT NULL REFERENCES receipt(id) ON DELETE CASCADE,
    product_id              BIGINT      NOT NULL REFERENCES product(id),
    purchase_order_item_id  BIGINT      REFERENCES purchase_order_item(id),
    source                  receipt_item_source NOT NULL,
    qty                     NUMERIC(12,3) NOT NULL CHECK (qty > 0),
    net_unit_price          NUMERIC(14,4),               -- rendelésről örökölt; kívülinél NULL
    sku_snapshot            TEXT        NOT NULL,
    ean_snapshot            TEXT,
    name_snapshot           TEXT        NOT NULL,
    unit                    TEXT        NOT NULL DEFAULT 'db',
    vat_name                TEXT,
    note                    TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_receipt_item_receipt ON receipt_item (receipt_id);
CREATE INDEX idx_receipt_item_poitem  ON receipt_item (purchase_order_item_id);

-- ------------------------------------------------------------
-- Félretett (ismeretlen) beolvasások
-- "Tedd félre, fel kell venni a terméket!"
-- Naplózzuk, hogy az admin lássa, mit kell rendezni a Naturasoftban.
-- ------------------------------------------------------------
CREATE TABLE unknown_scan (
    id              BIGSERIAL PRIMARY KEY,
    receipt_id      BIGINT      NOT NULL REFERENCES receipt(id) ON DELETE CASCADE,
    raw_code        TEXT        NOT NULL,                -- a beolvasott EAN / bemondott szöveg
    scanned_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved        BOOLEAN     NOT NULL DEFAULT FALSE
);

-- ------------------------------------------------------------
-- Terméktörzs import napló (admin átláthatóság)
-- ------------------------------------------------------------
CREATE TABLE product_import_log (
    id              BIGSERIAL PRIMARY KEY,
    filename        TEXT,
    rows_total      INT,
    rows_created    INT,
    rows_updated    INT,
    rows_skipped    INT,
    warnings        JSONB,
    imported_by     BIGINT      REFERENCES app_user(id),
    imported_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- Kézi migrációk
-- Ha a séma változik, ide kerül az ALTER parancs ÉS a fenti
-- CREATE TABLE is frissül, hogy egy fájlból felállítható legyen
-- egy új adatbázis.
-- ============================================================

-- 2026-09-05: hivatkozási szám a bevételezéshez
-- ALTER TABLE receipt ADD COLUMN reference_number TEXT;

-- 2026-09-05: Unas terméktörzs támogatás
-- CREATE TYPE product_source AS ENUM ('naturasoft', 'unas', 'order');
-- ALTER TABLE product ADD COLUMN in_naturasoft BOOLEAN NOT NULL DEFAULT FALSE;
-- ALTER TABLE product ADD COLUMN source product_source NOT NULL DEFAULT 'naturasoft';
-- A már meglévő termékek a Naturasoftból jöttek:
-- UPDATE product SET in_naturasoft = TRUE;
