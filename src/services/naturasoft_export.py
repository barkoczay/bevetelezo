"""Bevételezés -> Naturasoft import Excel.

FORMÁTUM: Excel 97-2003 (.xls, BIFF8) — a Naturasoft "Sorok beemelése
Excel fájl alapján" varázslója ezt várja, az .xlsx-et nem ismeri fel.
Íráshoz `xlwt` kell (az openpyxl csak .xlsx-et tud).

A varázsló oszlop-hozzárendelést kér, tehát a szerkezetet mi határozzuk
meg. FIX oszlopsorrend, hogy a leképezést egyszer kelljen beállítani.

Varázsló beállítások:
  Termék azonosítása : cikkszám alapján
  Beszerzési ár      : nettó
  Raktár neve        : legördülőből (nem oszlopból)
"""

from __future__ import annotations

import io
from collections import defaultdict
from datetime import datetime
from decimal import Decimal

import xlwt
from sqlalchemy.orm import Session

from src.db.models import Receipt, ReceiptStatus
from src.services.fifo import commit_receipt_to_orders

COLUMNS = [
    "Cikkszám",             # A - kötelező, ezen azonosít a Naturasoft
    "Megnevezés",           # B
    "Termékkód",            # C - EAN
    "Mennyiség",            # D
    "Nettó beszerzési ár",  # E
    "ÁFA kulcs neve",       # F
    "Megjegyzés",           # G
]

COLUMN_WIDTHS = [16, 60, 18, 11, 18, 16, 28]

# xlwt oszlopszélesség: 256 egység = 1 karakter
_WIDTH_UNIT = 256

# A letöltéshez tartozó MIME típus (FastAPI Response media_type)
XLS_MEDIA_TYPE = "application/vnd.ms-excel"


def build_workbook(receipt: Receipt) -> bytes:
    """Excel (.xls) előállítása a bevételezés tételeiből.

    Aggregálás kulcsa: cikkszám + nettó ár.

    Az árat SOHA nem módosítjuk és nem átlagoljuk — az a rendeléstételről
    öröklődik. Ha ugyanaz a cikkszám két rendelésről eltérő áron érkezett,
    két külön sorként megy ki.
    """
    groups: dict[tuple[str, str | None], dict] = {}
    order_refs: dict[tuple[str, str | None], set[str]] = defaultdict(set)

    for item in receipt.items:
        price = None if item.net_unit_price is None else str(item.net_unit_price)
        key = (item.sku_snapshot, price)

        if key not in groups:
            groups[key] = {
                "sku": item.sku_snapshot,
                "name": item.name_snapshot,
                "ean": item.ean_snapshot,
                "qty": Decimal(0),
                "price": item.net_unit_price,
                "vat_name": item.vat_name,
            }
        groups[key]["qty"] += Decimal(item.qty)

        if item.po_item is not None and item.po_item.order is not None:
            order_refs[key].add(item.po_item.order.order_number)

    wb = xlwt.Workbook(encoding="utf-8")
    ws = wb.add_sheet("Bevételezés")

    header_style = xlwt.easyxf("font: bold on")
    # A vonalkódot szövegként írjuk, hogy a vezető nullák megmaradjanak
    text_style = xlwt.easyxf(num_format_str="@")

    for col_idx, header in enumerate(COLUMNS):
        ws.write(0, col_idx, header, header_style)
        ws.col(col_idx).width = COLUMN_WIDTHS[col_idx] * _WIDTH_UNIT

    for row_idx, (key, g) in enumerate(groups.items(), start=1):
        refs = sorted(order_refs.get(key, []))
        note = f"Megrendelés: {', '.join(refs)}" if refs else "Rendelésen kívüli"

        ws.write(row_idx, 0, g["sku"])
        ws.write(row_idx, 1, g["name"])
        ws.write(row_idx, 2, g["ean"] or "", text_style)
        ws.write(row_idx, 3, float(g["qty"]))
        # Ár nélküli (rendelésen kívüli) tételnél a cellát üresen hagyjuk.
        # Üres szöveget nem írunk: a Naturasoft számot vár ebben az
        # oszlopban, és a szöveges cella hibát okozhat az importnál.
        if g["price"] is not None:
            ws.write(row_idx, 4, float(g["price"]))
        ws.write(row_idx, 5, g["vat_name"] or "")
        ws.write(row_idx, 6, note)

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def export_receipt(db: Session, receipt: Receipt, user_id: int) -> tuple[bytes, str]:
    """Export + lezárás.

    Ez a lezárás pillanata: innentől a bevételezés nem szerkeszthető,
    és a rendelések `received_qty` értéke megnő.
    """
    if receipt.status == ReceiptStatus.exported:
        raise ValueError("Ez a bevételezés már exportálva lett.")
    if not receipt.items:
        raise ValueError("A bevételezés nem tartalmaz tételt.")

    content = build_workbook(receipt)

    commit_receipt_to_orders(db, receipt)

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    supplier = (
        receipt.supplier.name.replace(" ", "_") if receipt.supplier else "ismeretlen"
    )
    filename = f"bevetelezes_{receipt.id}_{supplier}_{stamp}.xls"

    receipt.status = ReceiptStatus.exported
    receipt.exported_at = datetime.now()
    receipt.exported_by = user_id
    receipt.export_filename = filename
    db.commit()

    return content, filename
