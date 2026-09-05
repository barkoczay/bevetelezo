"""Egyszeri inicializáló végpont.

A táblákat és az első felhasználót hozza létre, hogy ne kelljen psql-t
vagy külön adatbázis-klienst használni.

Védelem: a hívónak ismernie kell a SETUP_TOKEN környezeti változó
értékét. Ha ez nincs beállítva, a végpont NEM működik — így egy
elfelejtett route sem jelent kockázatot.

Az inicializálás után a SETUP_TOKEN változó nyugodtan törölhető.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from src.auth import hash_password
from src.config import get_settings
from src.db.models import AppUser, Base
from src.db.session import engine, get_db

router = APIRouter(prefix="/api/setup", tags=["rendszer"])


def _check_token(token: str) -> None:
    configured = get_settings().setup_token
    if not configured:
        raise HTTPException(
            404, "Az inicializálás ki van kapcsolva (nincs SETUP_TOKEN beállítva)."
        )
    if token != configured:
        raise HTTPException(403, "Érvénytelen setup token.")


@router.post("/init-db")
def init_db(token: str = Query(...)) -> dict:
    """Létrehozza a hiányzó táblákat.

    Meglévő táblát nem módosít és nem töröl — ismételten is biztonságosan
    meghívható.
    """
    _check_token(token)

    before = set(inspect(engine).get_table_names())

    # A trigram keresés bővítménye a terméknévhez.
    # Ha a felhasználónak nincs joga hozzá, az nem végzetes: a keresés
    # ILIKE-kal ilyenkor is működik, csak lassabban.
    extension_note = "pg_trgm létrehozva"
    if engine.dialect.name == "postgresql":
        try:
            with engine.begin() as conn:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        except Exception as exc:  # noqa: BLE001
            extension_note = f"pg_trgm kihagyva: {exc}"

    Base.metadata.create_all(engine)

    after = set(inspect(engine).get_table_names())

    return {
        "status": "ok",
        "created_tables": sorted(after - before),
        "existing_tables": sorted(before),
        "extension": extension_note,
    }


@router.post("/create-user")
def create_first_user(
    username: str = Query(...),
    display_name: str = Query(...),
    password: str = Query(..., min_length=6),
    token: str = Query(...),
    db: Session = Depends(get_db),
) -> dict:
    """Felhasználó létrehozása vagy jelszavának felülírása."""
    _check_token(token)

    user = db.scalar(select(AppUser).where(AppUser.username == username))
    if user is None:
        user = AppUser(
            username=username,
            display_name=display_name,
            password_hash=hash_password(password),
        )
        db.add(user)
        action = "létrehozva"
    else:
        user.display_name = display_name
        user.password_hash = hash_password(password)
        user.active = True
        action = "frissítve"

    db.commit()
    return {"status": "ok", "username": username, "action": action}


@router.get("/status")
def setup_status(token: str = Query(...), db: Session = Depends(get_db)) -> dict:
    """Gyors ellenőrzés: megvannak-e a táblák, van-e felhasználó."""
    _check_token(token)

    tables = sorted(inspect(engine).get_table_names())
    user_count = 0
    if "app_user" in tables:
        user_count = len(list(db.scalars(select(AppUser))))

    return {"tables": tables, "user_count": user_count}
