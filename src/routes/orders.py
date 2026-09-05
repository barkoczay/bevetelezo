from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.auth import current_user
from src.db.models import (
    AppUser,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
    Receipt,
    ReceiptItem,
    ReceiptStatus,
)
from src.db.session import get_db
from src.schemas.api import (
    OrderDetailOut,
    OrderItemOut,
    OrderOut,
    OrderPreviewOut,
    OrderUpdateIn,
)
from src.services.order_import import extract_order_number, import_order, parse_order_file

router = APIRouter(prefix="/api/orders", tags=["megrendelések"])


def _to_order_out(order: PurchaseOrder, db: Session, with_items: bool = False):
    items = order.items
    data = {
        "id": order.id,
        "order_number": order.order_number,
        "order_date": order.order_date,
        "supplier_id": order.supplier_id,
        "supplier_name": order.supplier.name if order.supplier else None,
        "warehouse": order.warehouse,
        "status": order.status.value,
        "closed_manually": order.closed_manually,
        "uploaded_at": order.uploaded_at,
        "note": order.note,
        "item_count": len(items),
        "completed_item_count": sum(1 for i in items if i.remaining_qty <= 0),
        "ordered_total": sum((Decimal(i.ordered_qty) for i in items), Decimal(0)),
        "received_total": sum((Decimal(i.received_qty) for i in items), Decimal(0)),
    }
    if not with_items:
        return OrderOut(**data)

    data["items"] = [
        OrderItemOut(
            id=i.id,
            naturasoft_id=i.naturasoft_id,
            sku_snapshot=i.sku_snapshot,
            ean_snapshot=i.ean_snapshot,
            name_snapshot=i.name_snapshot,
            unit=i.unit,
            ordered_qty=i.ordered_qty,
            received_qty=i.received_qty,
            remaining_qty=i.remaining_qty,
            net_unit_price=i.net_unit_price,
            line_no=i.line_no,
        )
        for i in sorted(items, key=lambda x: x.line_no)
    ]
    return OrderDetailOut(**data)


@router.post("/preview", response_model=OrderPreviewOut)
def preview_order(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: AppUser = Depends(current_user),
):
    """Feltöltés előtti előnézet.

    A rendelésszám a fájlnévből jön, de a felületen javítható — ha valaki
    átnevezte a fájlt, vagy a Naturasoft más formátumot ad.
    """
    filename = file.filename or ""
    content = file.file.read()
    try:
        parsed = parse_order_file(content, filename)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    order_number = extract_order_number(filename)
    exists = False
    if order_number:
        exists = (
            db.scalar(
                select(PurchaseOrder).where(PurchaseOrder.order_number == order_number)
            )
            is not None
        )

    return OrderPreviewOut(
        order_number=order_number,
        warehouse=parsed.warehouse,
        item_count=len(parsed.items),
        items=[
            {
                "naturasoft_id": i.naturasoft_id,
                "sku": i.sku,
                "ean": i.ean,
                "name": i.name,
                "ordered_qty": float(i.ordered_qty),
                "net_unit_price": float(i.net_unit_price) if i.net_unit_price else None,
            }
            for i in parsed.items
        ],
        warnings=parsed.warnings,
        already_exists=exists,
    )


@router.post("/upload", response_model=OrderDetailOut)
def upload_order(
    file: UploadFile = File(...),
    order_number: str = Form(...),
    order_date: date = Form(...),
    supplier_id: int = Form(...),
    overwrite: bool = Form(False),
    db: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
):
    """Megrendelés mentése.

    `order_date` a FIFO alapja — nem a rendelésszám.
    """
    content = file.file.read()
    try:
        order = import_order(
            db,
            content=content,
            filename=file.filename or "",
            order_number=order_number.strip(),
            order_date=order_date,
            supplier_id=supplier_id,
            user_id=user.id,
            overwrite=overwrite,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return _to_order_out(order, db, with_items=True)


@router.get("", response_model=list[OrderOut])
def list_orders(
    status: str | None = None,
    supplier_id: int | None = None,
    q: str | None = None,
    db: Session = Depends(get_db),
    _: AppUser = Depends(current_user),
):
    stmt = select(PurchaseOrder).order_by(
        PurchaseOrder.order_date.desc(), PurchaseOrder.order_number.desc()
    )
    if status:
        stmt = stmt.where(PurchaseOrder.status == PurchaseOrderStatus(status))
    if supplier_id:
        stmt = stmt.where(PurchaseOrder.supplier_id == supplier_id)
    if q:
        stmt = stmt.where(PurchaseOrder.order_number.ilike(f"%{q.strip()}%"))
    return [_to_order_out(o, db) for o in db.scalars(stmt)]


@router.get("/{order_id}", response_model=OrderDetailOut)
def get_order(
    order_id: int, db: Session = Depends(get_db), _: AppUser = Depends(current_user)
):
    order = db.get(PurchaseOrder, order_id)
    if order is None:
        raise HTTPException(404, "Nincs ilyen megrendelés.")
    return _to_order_out(order, db, with_items=True)


@router.patch("/{order_id}", response_model=OrderDetailOut)
def update_order(
    order_id: int,
    payload: OrderUpdateIn,
    db: Session = Depends(get_db),
    _: AppUser = Depends(current_user),
):
    order = db.get(PurchaseOrder, order_id)
    if order is None:
        raise HTTPException(404, "Nincs ilyen megrendelés.")
    if payload.order_date is not None:
        order.order_date = payload.order_date
    if payload.supplier_id is not None:
        order.supplier_id = payload.supplier_id
    if payload.note is not None:
        order.note = payload.note
    db.commit()
    db.refresh(order)
    return _to_order_out(order, db, with_items=True)


@router.post("/{order_id}/close", response_model=OrderDetailOut)
def close_order(
    order_id: int, db: Session = Depends(get_db), _: AppUser = Depends(current_user)
):
    """Kézi lezárás: a maradék már nem fog megérkezni.

    Enélkül a részben teljesített rendelés örökre nyitva maradna, és a
    FIFO továbbra is allokálna rá.
    """
    order = db.get(PurchaseOrder, order_id)
    if order is None:
        raise HTTPException(404, "Nincs ilyen megrendelés.")
    order.status = PurchaseOrderStatus.closed
    order.closed_manually = True
    order.closed_at = datetime.now()
    db.commit()
    db.refresh(order)
    return _to_order_out(order, db, with_items=True)


@router.post("/{order_id}/reopen", response_model=OrderDetailOut)
def reopen_order(
    order_id: int, db: Session = Depends(get_db), _: AppUser = Depends(current_user)
):
    order = db.get(PurchaseOrder, order_id)
    if order is None:
        raise HTTPException(404, "Nincs ilyen megrendelés.")
    order.closed_manually = False
    order.closed_at = None
    received = sum(Decimal(i.received_qty) for i in order.items)
    order.status = (
        PurchaseOrderStatus.partial if received > 0 else PurchaseOrderStatus.open
    )
    db.commit()
    db.refresh(order)
    return _to_order_out(order, db, with_items=True)


@router.delete("/{order_id}", status_code=204)
def delete_order(
    order_id: int, db: Session = Depends(get_db), _: AppUser = Depends(current_user)
):
    """Törlés csak akkor, ha nincs rá bevételezés."""
    order = db.get(PurchaseOrder, order_id)
    if order is None:
        raise HTTPException(404, "Nincs ilyen megrendelés.")

    linked = db.scalar(
        select(ReceiptItem.id)
        .join(PurchaseOrderItem)
        .where(PurchaseOrderItem.purchase_order_id == order_id)
        .limit(1)
    )
    if linked is not None:
        raise HTTPException(
            409, "A megrendeléshez már tartozik bevételezés, nem törölhető."
        )

    db.delete(order)
    db.commit()
