"""Naturasoft Excel exportok beolvasásához közös segédfüggvények.

A Naturasoft legacy `.xls` (BIFF) formátumot ad, amit az openpyxl nem
olvas — ehhez az `xlrd >= 2.0.1` motor kell.
"""

from __future__ import annotations

import io
from decimal import Decimal, InvalidOperation

import pandas as pd


def read_excel(content: bytes, filename: str) -> pd.DataFrame:
    """Beolvas egy .xls vagy .xlsx fájlt fejléc nélkül, nyers formában.

    Fejléc nélkül olvasunk, hogy a fejlécsor helyét magunk kereshessük meg —
    a Naturasoft exportokban nem mindig az első sor.
    """
    engine = "xlrd" if filename.lower().endswith(".xls") else "openpyxl"
    return pd.read_excel(io.BytesIO(content), engine=engine, header=None, dtype=object)


def find_header_row(df: pd.DataFrame, required: set[str], max_scan: int = 10) -> int:
    """Megkeresi azt a sort, amelyik tartalmazza az összes megadott fejlécnevet."""
    for idx in range(min(max_scan, len(df))):
        values = {normalize_header(v) for v in df.iloc[idx].tolist()}
        if required <= values:
            return idx
    raise ValueError(
        f"Nem található fejlécsor. Elvárt oszlopok: {', '.join(sorted(required))}"
    )


def normalize_header(value) -> str:
    """Fejlécnév normalizálása: kisbetű, felesleges szóközök nélkül."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip().lower()


def build_column_map(df: pd.DataFrame, header_row: int) -> dict[str, int]:
    """Fejlécnév -> oszlopindex leképezés."""
    return {
        normalize_header(v): i
        for i, v in enumerate(df.iloc[header_row].tolist())
        if normalize_header(v)
    }


def cell(row, col_map: dict[str, int], header: str):
    """Egy cella nyers értéke fejlécnév alapján, hiányzó oszlopnál None."""
    idx = col_map.get(header)
    if idx is None or idx >= len(row):
        return None
    value = row.iloc[idx] if hasattr(row, "iloc") else row[idx]
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return value


def as_text(value) -> str | None:
    """Szöveggé alakítás.

    FONTOS: a pandas a számként tárolt kódokat float-ként hozza
    (5413470315539.0). Egyszerű str() esetén a ".0" is bekerülne, ezért
    az egész értékű float-okat előbb int-re alakítjuk.
    """
    if value is None:
        return None
    if isinstance(value, float):
        if pd.isna(value):
            return None
        if value.is_integer():
            return str(int(value))
        return str(value)
    if isinstance(value, int):
        return str(value)
    text = str(value).strip()
    return text or None


def as_code(value) -> str | None:
    """Vonalkód / cikkszám: szövegként, csak a szóközöket távolítjuk el.

    A kötőjelet MEG KELL ŐRIZNI: a Naturasoft cikkszámai tartalmazzák
    (pl. 'M-PQD0001Q'), és a bevételezés import ezen azonosít.

    Vezető nullákat nem vágunk le és nem is töltünk fel — a Naturasoft
    Termékkód mezőjében 13 jegyű EAN és rövidebb belső kód is előfordul.
    """
    text = as_text(value)
    if text is None:
        return None
    return text.replace(" ", "").replace("\xa0", "") or None


def as_decimal(value) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and pd.isna(value):
            return None
        return Decimal(str(value))
    text = str(value).strip().replace(" ", "").replace("\xa0", "")
    if not text:
        return None
    # magyar tizedesvessző
    text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def as_bool(value) -> bool:
    """A Naturasoft 'Igen' / üres formában adja a logikai mezőket."""
    text = as_text(value)
    if text is None:
        return False
    return text.strip().lower() in {"igen", "yes", "true", "1", "x"}
