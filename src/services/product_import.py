"""Terméktörzs import a Naturasoft 'Terméknyilvántartás' exportból.

Szabály: FRISSÍT + HOZZÁAD, soha nem töröl. Egy hibás vagy szűrt export
így nem tudja kiüríteni a törzset.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import Product, ProductImportLog, ProductSource
from src.services.excel_utils import (
    as_bool,
    as_code,
    as_decimal,
    as_text,
    build_column_map,
    cell,
    find_header_row,
    read_excel,
)

# A Naturasoft export oszlopnevei (kisbetűsítve)
H_SORSZAM = "sorszám"
H_MEGNEVEZES = "megnevezés"
H_GYARTO = "gyártók"
H_TERMEKKOD = "termékkód"
H_CIKKSZAM = "cikkszám"
H_MEE = "mee."
H_AFA = "áfa"
H_SULY = "súly (kg)"
H_CSOPORT = "termékcsoportok"
H_TOROLT = "törölt (inaktív)"

REQUIRED_HEADERS = {H_SORSZAM, H_MEGNEVEZES, H_CIKKSZAM}


@dataclass
class ProductImportResult:
    rows_total: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)


def import_products(
    db: Session, content: bytes, filename: str, user_id: int | None = None
) -> ProductImportResult:
    df = read_excel(content, filename)
    header_row = find_header_row(df, REQUIRED_HEADERS)
    col_map = build_column_map(df, header_row)

    result = ProductImportResult()
    ean_counter: Counter[str] = Counter()
    seen_skus: set[str] = set()

    for _, row in df.iloc[header_row + 1 :].iterrows():
        naturasoft_id_raw = cell(row, col_map, H_SORSZAM)
        sku = as_code(cell(row, col_map, H_CIKKSZAM))
        name = as_text(cell(row, col_map, H_MEGNEVEZES))

        # Üres vagy összesítő sor
        if naturasoft_id_raw is None and sku is None and name is None:
            continue

        result.rows_total += 1

        # A cikkszám a kulcs: enélkül a termék nem bevételezhető, mert a
        # Naturasoft importja is cikkszám alapján azonosít.
        if not sku:
            result.skipped += 1
            result.warnings.append(f"Hiányzó cikkszám: {name or '(névtelen sor)'}")
            continue

        if sku in seen_skus:
            result.skipped += 1
            result.warnings.append(f"Duplikált cikkszám a fájlban: {sku}")
            continue
        seen_skus.add(sku)

        naturasoft_id = as_decimal(naturasoft_id_raw)
        naturasoft_id = int(naturasoft_id) if naturasoft_id is not None else None

        ean = as_code(cell(row, col_map, H_TERMEKKOD))
        if ean:
            ean_counter[ean] += 1
        else:
            result.warnings.append(
                f"Nincs vonalkód ({sku}): {name} — csak kézi keresés"
            )

        values = {
            "naturasoft_id": naturasoft_id,
            "ean": ean,
            "name": name or sku,
            "manufacturer": as_text(cell(row, col_map, H_GYARTO)),
            "unit": as_text(cell(row, col_map, H_MEE)) or "db",
            "vat_rate": as_text(cell(row, col_map, H_AFA)),
            "weight_kg": as_decimal(cell(row, col_map, H_SULY)),
            "product_group": as_text(cell(row, col_map, H_CSOPORT)),
            "inactive": as_bool(cell(row, col_map, H_TOROLT)),
            # Ez a Naturasoft saját exportja, tehát a termék biztosan létezik ott.
            "in_naturasoft": True,
            "source": ProductSource.naturasoft,
        }

        existing = db.scalar(select(Product).where(Product.sku == sku))
        if existing is None:
            db.add(Product(sku=sku, **values))
            result.created += 1
        else:
            for key, value in values.items():
                # A meglévő vonalkódot nem írjuk felül üressel: lehet, hogy
                # egy megrendelésből pótoltuk, és a Naturasoftban még
                # mindig nincs kitöltve.
                if key == "ean" and value is None and existing.ean:
                    continue
                setattr(existing, key, value)
            result.updated += 1

    for ean, count in ean_counter.items():
        if count > 1:
            result.warnings.append(
                f"Ugyanaz a vonalkód {count} terméknél szerepel: {ean} — "
                "a szkennelés nem lesz egyértelmű"
            )

    db.add(
        ProductImportLog(
            filename=filename,
            rows_total=result.rows_total,
            rows_created=result.created,
            rows_updated=result.updated,
            rows_skipped=result.skipped,
            warnings={"items": result.warnings},
            imported_by=user_id,
        )
    )
    db.commit()
    return result
