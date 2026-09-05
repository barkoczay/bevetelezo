from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from src.auth import current_user
from src.db.models import (
    AppUser,
    Product,
    Receipt,
    ReceiptItem,
    ReceiptStatus,
    UnknownScan,
)
from src.db.session import get_db
from src.schemas.api import (
    ReceiptCreateIn,
    ReceiptDetailOut,
    ReceiptItemOut,
    ReceiptItemUpdateIn,
    ReceiptOut,
    ReceiptUpdateIn,
    ScanIn,
    ScanOut,
    UnknownScanOut,
)
from src.routes.core import _strip_separators, normalize_code
from src.services.fifo import apply_scan, recalculate
from src.services.naturasoft_export import XLS_MEDIA_TYPE, export_receipt

router = APIRouter(prefix="/api/receipts", tags=["bevételezés"])

MSG_SET_ASIDE = "Tedd félre, fel kell venni a terméket!"


def _find_product(db: Session, code: str) -> Product | None:
    """Termék feloldása vonalkód vagy cikkszám alapján.

    A vonalkódot pontosan hasonlítjuk (a szkenner mindig ugyanazt küldi),
    a cikkszámnál viszont az elválasztó karakterek nem számítanak —
    'CVAMPI' megtalálja a 'CVA-MPI'-t.
    """
    code = code.strip()
    product = db.scalar(
        select(Product).where(or_(Product.ean == code, Product.sku == code))
    )
    if product is not None:
        return product

    normalized = normalize_code(code)
    if not normalized:
        return None
    # A cikkszámnál a kis- és nagybetű sem számít: 'cva-mpi' = 'CVA-MPI'
    return db.scalar(
        select(Product).where(
            func.lower(_strip_separators(Product.sku)) == normalized.lower()
        )
    )


def _item_out(item: ReceiptItem) -> ReceiptItemOut:
    return ReceiptItemOut(
        id=item.id,
        product_id=item.product_id,
        purchase_order_item_id=item.purchase_order_item_id,
        source=item.source.value,
        qty=item.qty,
        net_unit_price=item.net_unit_price,
        sku_snapshot=item.sku_snapshot,
        ean_snapshot=item.ean_snapshot,
        name_snapshot=item.name_snapshot,
        unit=item.unit,
        vat_name=item.vat_name,
        note=item.note,
        missing_in_naturasoft=(
            item.product is not None and not item.product.in_naturasoft
        ),
        order_number=(
            item.po_item.order.order_number
            if item.po_item is not None and item.po_item.order is not None
            else None
        ),
    )


def _receipt_out(receipt: Receipt, with_items: bool = False):
    data = {
        "id": receipt.id,
        "supplier_id": receipt.supplier_id,
        "supplier_name": receipt.supplier.name if receipt.supplier else None,
        "status": receipt.status.value,
        "reference_number": receipt.reference_number,
        "suggested_reference": receipt.suggested_reference,
        "delivery_note_no": receipt.delivery_note_no,
        "created_at": receipt.created_at,
        "scanned_at": receipt.scanned_at,
        "exported_at": receipt.exported_at,
        "export_filename": receipt.export_filename,
        "note": receipt.note,
        "item_count": len(receipt.items),
        "unknown_count": len(receipt.unknown_scans),
        "missing_in_naturasoft_count": sum(
            1
            for i in receipt.items
            if i.product is not None and not i.product.in_naturasoft
        ),
    }
    if not with_items:
        return ReceiptOut(**data)
    data["items"] = [_item_out(i) for i in receipt.items]
    data["unknown_scans"] = [
        UnknownScanOut.model_validate(u) for u in receipt.unknown_scans
    ]
    return ReceiptDetailOut(**data)


def _get_editable(db: Session, receipt_id: int) -> Receipt:
    receipt = db.get(Receipt, receipt_id)
    if receipt is None:
        raise HTTPException(404, "Nincs ilyen bevételezés.")
    if not receipt.editable:
        raise HTTPException(
            409, "Ez a bevételezés már exportálva lett, nem módosítható."
        )
    return receipt


# ---------------------------------------------------------------- raktáros


@router.post("", response_model=ReceiptDetailOut)
def create_receipt(
    payload: ReceiptCreateIn,
    db: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
):
    receipt = Receipt(
        supplier_id=payload.supplier_id,
        delivery_note_no=payload.delivery_note_no,
        created_by=user.id,
        locked_by=user.id,
    )
    db.add(receipt)
    db.commit()
    db.refresh(receipt)
    return _receipt_out(receipt, with_items=True)


@router.post("/{receipt_id}/scan", response_model=ScanOut)
def scan(
    receipt_id: int,
    payload: ScanIn,
    db: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
):
    """Egy beolvasás feldolgozása.

    A raktáros ebből csak a termék nevét és a mennyiséget látja — hogy
    melyik rendelésre könyvelődött, az nem az ő dolga.
    """
    receipt = _get_editable(db, receipt_id)
    if receipt.locked_by not in (None, user.id):
        raise HTTPException(409, "Ezen a bevételezésen már dolgozik valaki.")

    code = payload.code.strip()
    product = _find_product(db, code)

    if product is None:
        db.add(UnknownScan(receipt_id=receipt.id, raw_code=code))
        db.commit()
        return ScanOut(status="unknown", message=MSG_SET_ASIDE)

    if product.inactive:
        return ScanOut(
            status="inactive",
            message=f"{product.name}: inaktív termék, tedd félre!",
            product_name=product.name,
        )

    items = apply_scan(db, receipt, product, Decimal(payload.qty))
    db.commit()

    total = sum(
        (Decimal(i.qty) for i in receipt.items if i.product_id == product.id),
        Decimal(0),
    )
    return ScanOut(
        status="ok",
        product_name=product.name,
        unit=product.unit,
        total_qty=total,
        item_ids=[i.id for i in items],
    )


@router.post("/{receipt_id}/set-quantity", response_model=ScanOut)
def set_quantity(
    receipt_id: int,
    payload: ScanIn,
    db: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
):
    """A termék PONTOS mennyiségének beállítása ebben a bevételezésben.

    A FIFO miatt egy termék több rendeléstételre is eshet, ezért a meglévő
    sorokat eldobjuk és újraallokálunk. Így a csökkentés is helyes marad.
    """
    receipt = _get_editable(db, receipt_id)
    if receipt.locked_by not in (None, user.id):
        raise HTTPException(409, "Ezen a bevételezésen már dolgozik valaki.")

    product = _find_product(db, payload.code)
    if product is None:
        raise HTTPException(404, "Nincs ilyen termék.")

    qty = Decimal(payload.qty)
    if qty < 0:
        raise HTTPException(400, "A mennyiség nem lehet negatív.")

    touched: list[int] = []
    for item in list(receipt.items):
        if item.product_id == product.id:
            touched.append(item.purchase_order_item_id)
            db.delete(item)
    db.flush()
    recalculate(db, touched)   # előbb felszabadítjuk, hogy legyen mihez allokálni

    items = []
    if qty > 0:
        items = apply_scan(db, receipt, product, qty)
    db.commit()
    db.refresh(receipt)

    return ScanOut(
        status="ok",
        product_name=product.name,
        unit=product.unit,
        total_qty=qty,
        item_ids=[i.id for i in items],
    )


@router.delete("/{receipt_id}/products/{code}", response_model=ScanOut)
def remove_product(
    receipt_id: int,
    code: str,
    db: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
):
    """Téves beolvasás visszavonása: a termék összes sora törlődik.

    A visszavont mennyiség azonnal visszakerül a megrendelés maradékába.
    """
    receipt = _get_editable(db, receipt_id)
    if receipt.locked_by not in (None, user.id):
        raise HTTPException(409, "Ezen a bevételezésen már dolgozik valaki.")

    product = _find_product(db, code)
    if product is None:
        raise HTTPException(404, "Nincs ilyen termék.")

    removed = 0
    touched: list[int] = []
    for item in list(receipt.items):
        if item.product_id == product.id:
            touched.append(item.purchase_order_item_id)
            db.delete(item)
            removed += 1
    db.flush()
    recalculate(db, touched)
    db.commit()

    if not removed:
        raise HTTPException(404, "Ez a termék nincs a bevételezésben.")

    return ScanOut(
        status="ok",
        product_name=product.name,
        unit=product.unit,
        total_qty=Decimal(0),
    )


@router.post("/{receipt_id}/finish", response_model=ReceiptDetailOut)
def finish_scanning(
    receipt_id: int,
    db: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
):
    """A raktáros végzett. Innentől az admin szerkesztheti."""
    receipt = _get_editable(db, receipt_id)
    receipt.status = ReceiptStatus.scanned
    receipt.scanned_at = datetime.now()
    receipt.locked_by = None
    db.commit()
    db.refresh(receipt)
    return _receipt_out(receipt, with_items=True)


# ---------------------------------------------------------------- admin


@router.get("", response_model=list[ReceiptOut])
def list_receipts(
    status: str | None = None,
    supplier_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    _: AppUser = Depends(current_user),
):
    """Bevételezések, legfrissebb elöl. Lapozható."""
    stmt = select(Receipt).order_by(Receipt.created_at.desc(), Receipt.id.desc())
    if status:
        stmt = stmt.where(Receipt.status == ReceiptStatus(status))
    if supplier_id:
        stmt = stmt.where(Receipt.supplier_id == supplier_id)

    stmt = stmt.limit(max(1, min(limit, 200))).offset(max(0, offset))
    return [_receipt_out(r) for r in db.scalars(stmt)]


@router.get("/{receipt_id}", response_model=ReceiptDetailOut)
def get_receipt(
    receipt_id: int, db: Session = Depends(get_db), _: AppUser = Depends(current_user)
):
    receipt = db.get(Receipt, receipt_id)
    if receipt is None:
        raise HTTPException(404, "Nincs ilyen bevételezés.")
    return _receipt_out(receipt, with_items=True)


@router.patch("/{receipt_id}", response_model=ReceiptDetailOut)
def update_receipt(
    receipt_id: int,
    payload: ReceiptUpdateIn,
    db: Session = Depends(get_db),
    _: AppUser = Depends(current_user),
):
    receipt = _get_editable(db, receipt_id)
    if payload.reference_number is not None:
        receipt.reference_number = payload.reference_number or None
    if payload.delivery_note_no is not None:
        receipt.delivery_note_no = payload.delivery_note_no or None
    if payload.note is not None:
        receipt.note = payload.note or None
    db.commit()
    db.refresh(receipt)
    return _receipt_out(receipt, with_items=True)


@router.patch("/{receipt_id}/items/{item_id}", response_model=ReceiptItemOut)
def update_item(
    receipt_id: int,
    item_id: int,
    payload: ReceiptItemUpdateIn,
    db: Session = Depends(get_db),
    _: AppUser = Depends(current_user),
):
    _get_editable(db, receipt_id)
    item = db.get(ReceiptItem, item_id)
    if item is None or item.receipt_id != receipt_id:
        raise HTTPException(404, "Nincs ilyen tétel ebben a bevételezésben.")
    if payload.qty is not None:
        item.qty = payload.qty
    if payload.net_unit_price is not None:
        item.net_unit_price = payload.net_unit_price
    if payload.note is not None:
        item.note = payload.note or None
    db.flush()
    recalculate(db, [item.purchase_order_item_id])
    db.commit()
    db.refresh(item)
    return _item_out(item)


@router.delete("/{receipt_id}/items/{item_id}", status_code=204)
def delete_item(
    receipt_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    _: AppUser = Depends(current_user),
):
    """Tétel törlése.

    A mennyiség azonnal visszakerül a megrendelés maradékába, tehát a
    következő bevételezésnél újra megjelenik.
    """
    _get_editable(db, receipt_id)
    item = db.get(ReceiptItem, item_id)
    if item is None or item.receipt_id != receipt_id:
        raise HTTPException(404, "Nincs ilyen tétel ebben a bevételezésben.")
    po_item_id = item.purchase_order_item_id
    db.delete(item)
    db.flush()
    recalculate(db, [po_item_id])
    db.commit()


@router.post("/{receipt_id}/reopen", response_model=ReceiptDetailOut)
def reopen_receipt(
    receipt_id: int,
    db: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
):
    """Beolvasott bevételezés visszanyitása, hogy folytatható legyen.

    Exportált bevételezést nem lehet visszanyitni: az már bekerült a
    Naturasoftba, ott kell javítani.
    """
    receipt = _get_editable(db, receipt_id)
    receipt.status = ReceiptStatus.in_progress
    receipt.scanned_at = None
    receipt.locked_by = user.id
    db.commit()
    db.refresh(receipt)
    return _receipt_out(receipt, with_items=True)


@router.delete("/{receipt_id}", status_code=204)
def delete_receipt(
    receipt_id: int,
    db: Session = Depends(get_db),
    _: AppUser = Depends(current_user),
):
    """Bevételezés törlése.

    A tételei mennyisége visszakerül a megrendelések maradékába.
    Exportált bevételezés nem törölhető: az már a Naturasoftban is
    szerepel, a törlés eltüntetné a nyomát.
    """
    receipt = db.get(Receipt, receipt_id)
    if receipt is None:
        raise HTTPException(404, "Nincs ilyen bevételezés.")
    if receipt.status == ReceiptStatus.exported:
        raise HTTPException(
            409,
            "Exportált bevételezés nem törölhető. Ha hibás, a Naturasoftban kell javítani.",
        )

    touched = [i.purchase_order_item_id for i in receipt.items]
    db.delete(receipt)
    db.flush()
    recalculate(db, touched)
    db.commit()


@router.post("/{receipt_id}/export")
def export(
    receipt_id: int,
    db: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
):
    """Naturasoft import Excel (.xls) generálása + lezárás.

    EZ a lezárás pillanata: a bevételezés zárolódik, és a rendelések
    `received_qty` értéke megnő.
    """
    receipt = db.get(Receipt, receipt_id)
    if receipt is None:
        raise HTTPException(404, "Nincs ilyen bevételezés.")

    # Ha az admin nem adott meg sajátot, az érintett rendelésszámok kerülnek be
    if not receipt.reference_number:
        receipt.reference_number = receipt.suggested_reference

    try:
        content, filename = export_receipt(db, receipt, user.id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))

    return Response(
        content=content,
        media_type=XLS_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
