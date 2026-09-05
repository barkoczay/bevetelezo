from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from src.auth import authenticate, create_token, current_user
from src.db.models import AppUser, Product, Supplier
from src.db.session import get_db
from src.schemas.api import (
    ProductImportOut,
    ProductOut,
    SupplierIn,
    SupplierOut,
    TokenOut,
)
from src.services.product_import import import_products

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------- auth


@router.post("/auth/login", response_model=TokenOut, tags=["auth"])
def login(
    form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)
) -> TokenOut:
    user = authenticate(db, form.username, form.password)
    if user is None:
        raise HTTPException(401, "Hibás felhasználónév vagy jelszó.")
    return TokenOut(access_token=create_token(user), display_name=user.display_name)


@router.get("/auth/me", tags=["auth"])
def me(user: AppUser = Depends(current_user)) -> dict:
    return {"id": user.id, "username": user.username, "display_name": user.display_name}


# ---------------------------------------------------------------- szállítók


@router.get("/suppliers", response_model=list[SupplierOut], tags=["szállítók"])
def list_suppliers(
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    _: AppUser = Depends(current_user),
):
    stmt = select(Supplier).order_by(Supplier.name)
    if not include_inactive:
        stmt = stmt.where(Supplier.active.is_(True))
    return list(db.scalars(stmt))


@router.post("/suppliers", response_model=SupplierOut, tags=["szállítók"])
def create_supplier(
    payload: SupplierIn,
    db: Session = Depends(get_db),
    _: AppUser = Depends(current_user),
):
    existing = db.scalar(select(Supplier).where(Supplier.name == payload.name))
    if existing is not None:
        raise HTTPException(409, f"Ilyen nevű szállító már létezik: {payload.name}")
    supplier = Supplier(name=payload.name, active=payload.active)
    db.add(supplier)
    db.commit()
    db.refresh(supplier)
    return supplier


@router.patch("/suppliers/{supplier_id}", response_model=SupplierOut, tags=["szállítók"])
def update_supplier(
    supplier_id: int,
    payload: SupplierIn,
    db: Session = Depends(get_db),
    _: AppUser = Depends(current_user),
):
    supplier = db.get(Supplier, supplier_id)
    if supplier is None:
        raise HTTPException(404, "Nincs ilyen szállító.")
    supplier.name = payload.name
    supplier.active = payload.active
    db.commit()
    db.refresh(supplier)
    return supplier


# ---------------------------------------------------------------- termékek


@router.get("/products/search", response_model=list[ProductOut], tags=["termékek"])
def search_products(
    q: str,
    limit: int = 20,
    db: Session = Depends(get_db),
    _: AppUser = Depends(current_user),
):
    """Kézi és hangos kereséshez.

    A hangfelismerés pontatlanságát a részszavas keresés tolerálja: elég
    a név egy töredéke vagy a cikkszám vége.
    """
    term = q.strip()
    if not term:
        return []

    pattern = f"%{term}%"
    stmt = (
        select(Product)
        .where(
            Product.inactive.is_(False),
            or_(
                Product.name.ilike(pattern),
                Product.sku.ilike(pattern),
                Product.ean.ilike(pattern),
            ),
        )
        .order_by(func.length(Product.name))
        .limit(limit)
    )
    return list(db.scalars(stmt))


@router.post("/products/import", response_model=ProductImportOut, tags=["termékek"])
def upload_product_master(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
):
    """Terméktörzs import a Naturasoft exportból.

    FRISSÍT + HOZZÁAD, soha nem töröl.
    """
    content = file.file.read()
    try:
        result = import_products(db, content, file.filename or "termektorzs.xls", user.id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return ProductImportOut(
        rows_total=result.rows_total,
        created=result.created,
        updated=result.updated,
        skipped=result.skipped,
        warnings=result.warnings,
    )
