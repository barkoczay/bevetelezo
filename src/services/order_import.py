"""Szállítói megrendelés import a Naturasoft tétellista exportból.

A rendelésszám a FÁJLNÉVBEN van, a tartalomban nem:
    'Szállítói_megrendelés__9686__tételek_listája.xls'  ->  9686

A dátumot és a szállítót a felhasználó adja meg a feltöltéskor
(az export egyiket sem tartalmazza). A dátum a FIFO alapja.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import Product, PurchaseOrder, PurchaseOrderItem, PurchaseOrderStatus
from src.services.excel_utils import (
    as_code,
    as_decimal,
    as_text,
    build_column_map,
    cell,
    find_header_row,
    read_excel,
)

H_SORSZAM = "termék sorszám"
H_MEGNEVEZES = "megnevezés"
H_TERMEKKOD = "termékkód"
H_CIKKSZAM = "cikkszám"
H_MENNYISEG = "mennyiség"
H_MEE = "mee."
H_NETTO_EGYSEGAR = "nettó egységár"
H_AFA = "áfa%"
H_RAKTAR = "raktár"
H_MEGJEGYZES = "megjegyzés"

REQUIRED_HEADERS = {H_SORSZAM, H_MEGNEVEZES, H_CIKKSZAM, H_MENNYISEG}

# 'Szállítói_megrendelés__9686__tételek_listája.xls'
ORDER_NUMBER_PATTERN = re.compile(r"megrendel[ée]s__(\d+)__", re.IGNORECASE)


def extract_order_number(filename: str) -> str | None:
    """Rendelésszám kinyerése a fájlnévből.

    A felületen ez előre kitöltve jelenik meg, de javítható — ha valaki
    átnevezte a fájlt, vagy a Naturasoft más formátumot ad.
    """
    match = ORDER_NUMBER_PATTERN.search(filename)
    if match:
        return match.group(1)
    # tartalék: az első számsorozat a fájlnévben
    fallback = re.search(r"(\d{3,})", filename)
    return fallback.group(1) if fallback else None


@dataclass
class ParsedOrderItem:
    naturasoft_id: int
    sku: str
    ean: str | None
    name: str
    unit: str
    ordered_qty: Decimal
    net_unit_price: Decimal | None
    vat_rate: str | None
    line_no: int


@dataclass
class ParsedOrder:
    warehouse: str | None = None
    items: list[ParsedOrderItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def parse_order_file(content: bytes, filename: str) -> ParsedOrder:
    df = read_excel(content, filename)
    header_row = find_header_row(df, REQUIRED_HEADERS)
    col_map = build_column_map(df, header_row)

    parsed = ParsedOrder()
    line_no = 0

    for _, row in df.iloc[header_row + 1 :].iterrows():
        naturasoft_id_raw = cell(row, col_map, H_SORSZAM)
        sku = as_code(cell(row, col_map, H_CIKKSZAM))

        # Az utolsó sor összesítő: nincs benne termék sorszám és cikkszám,
        # csak a mennyiség és az összegek. Ezt ki kell hagyni.
        if naturasoft_id_raw is None or sku is None:
            continue

        naturasoft_id = as_decimal(naturasoft_id_raw)
        if naturasoft_id is None:
            continue

        qty = as_decimal(cell(row, col_map, H_MENNYISEG))
        if qty is None or qty <= 0:
            parsed.warnings.append(f"Nulla vagy hiányzó mennyiség: {sku}")
            continue

        line_no += 1
        parsed.items.append(
            ParsedOrderItem(
                naturasoft_id=int(naturasoft_id),
                sku=sku,
                ean=as_code(cell(row, col_map, H_TERMEKKOD)),
                name=as_text(cell(row, col_map, H_MEGNEVEZES)) or sku,
                unit=as_text(cell(row, col_map, H_MEE)) or "db",
                ordered_qty=qty,
                net_unit_price=as_decimal(cell(row, col_map, H_NETTO_EGYSEGAR)),
                vat_rate=as_text(cell(row, col_map, H_AFA)),
                line_no=line_no,
            )
        )

        if parsed.warehouse is None:
            parsed.warehouse = as_text(cell(row, col_map, H_RAKTAR))

    if not parsed.items:
        raise ValueError("A fájl nem tartalmaz értékelhető tételsort.")

    return parsed


def import_order(
    db: Session,
    content: bytes,
    filename: str,
    order_number: str,
    order_date: date,
    supplier_id: int,
    user_id: int | None = None,
    overwrite: bool = False,
) -> PurchaseOrder:
    """Rendelés mentése.

    Ha a rendelésszám már létezik:
      - overwrite=False -> hiba (a felület rákérdez)
      - overwrite=True  -> csak akkor engedjük, ha még nincs rá bevételezés
    """
    parsed = parse_order_file(content, filename)

    existing = db.scalar(
        select(PurchaseOrder).where(PurchaseOrder.order_number == order_number)
    )
    if existing is not None:
        if not overwrite:
            raise ValueError(
                f"A(z) {order_number} számú megrendelés már fel van töltve."
            )
        if any(Decimal(i.received_qty) > 0 for i in existing.items):
            raise ValueError(
                f"A(z) {order_number} megrendelésre már történt bevételezés, "
                "nem írható felül."
            )
        db.delete(existing)
        db.flush()

    order = PurchaseOrder(
        order_number=order_number,
        order_date=order_date,
        supplier_id=supplier_id,
        warehouse=parsed.warehouse,
        status=PurchaseOrderStatus.open,
        source_filename=filename,
        uploaded_by=user_id,
    )
    db.add(order)
    db.flush()

    for item in parsed.items:
        product = db.scalar(
            select(Product).where(Product.naturasoft_id == item.naturasoft_id)
        )
        if product is None:
            parsed.warnings.append(
                f"A termék nincs a törzsben (sorszám {item.naturasoft_id}): "
                f"{item.name} — frissítsd a terméktörzset"
            )

        db.add(
            PurchaseOrderItem(
                purchase_order_id=order.id,
                product_id=product.id if product else None,
                naturasoft_id=item.naturasoft_id,
                sku_snapshot=item.sku,
                ean_snapshot=item.ean,
                name_snapshot=item.name,
                unit=item.unit,
                ordered_qty=item.ordered_qty,
                net_unit_price=item.net_unit_price,
                vat_rate=item.vat_rate,
                line_no=item.line_no,
            )
        )

    db.commit()
    db.refresh(order)
    return order
