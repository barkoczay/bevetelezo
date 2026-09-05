"""FIFO allokáció: a beolvasott mennyiség szétosztása a nyitott rendelések között.

A raktáros ebből semmit nem lát és nem is dönt — beolvas, megad egy
mennyiséget, a rendszer eldönti, melyik rendelésre könyveli.

Sorrend: `order_date`, majd azonos dátumnál `order_number`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import (
    Product,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
    Receipt,
    ReceiptItem,
    ReceiptItemSource,
    ReceiptStatus,
)


@dataclass
class Allocation:
    po_item: PurchaseOrderItem | None   # None = rendelésen kívüli
    qty: Decimal


def open_order_items(
    db: Session, supplier_id: int, product_id: int
) -> list[PurchaseOrderItem]:
    """Az adott szállító nyitott rendeléseinek tételei erre a termékre, FIFO sorrendben."""
    stmt = (
        select(PurchaseOrderItem)
        .join(PurchaseOrder)
        .where(
            PurchaseOrder.supplier_id == supplier_id,
            PurchaseOrder.status.in_(
                [PurchaseOrderStatus.open, PurchaseOrderStatus.partial]
            ),
            PurchaseOrderItem.product_id == product_id,
        )
        .order_by(PurchaseOrder.order_date, PurchaseOrder.order_number)
    )
    return list(db.scalars(stmt))


def pending_qty_on_po_item(db: Session, po_item_id: int, exclude_receipt_id: int) -> Decimal:
    """Más, még nem exportált bevételezésekben lefoglalt mennyiség.

    A `received_qty` csak exportkor nő, ezért a még nyitott bevételezések
    foglalásait külön kell figyelembe venni — különben két párhuzamos
    bevételezés ugyanarra a maradékra allokálna.
    """
    stmt = (
        select(ReceiptItem.qty)
        .join(Receipt)
        .where(
            ReceiptItem.purchase_order_item_id == po_item_id,
            ReceiptItem.receipt_id != exclude_receipt_id,
            Receipt.status != ReceiptStatus.exported,
        )
    )
    return sum((Decimal(q) for q in db.scalars(stmt)), Decimal(0))


def allocate(
    db: Session, receipt: Receipt, product: Product, qty: Decimal
) -> list[Allocation]:
    """Szétosztja a mennyiséget a nyitott rendeléstételek maradéka között.

    A túlcsorduló rész (vagy ha nincs nyitott rendelés) 'rendelésen kívüli'
    allokációként jelenik meg — ez nem hiba, csak jelzés az adminnak.
    """
    remaining_to_place = Decimal(qty)
    allocations: list[Allocation] = []

    for po_item in open_order_items(db, receipt.supplier_id, product.id):
        if remaining_to_place <= 0:
            break

        available = (
            po_item.remaining_qty
            - pending_qty_on_po_item(db, po_item.id, receipt.id)
            - _already_allocated_in_receipt(db, receipt.id, po_item.id)
        )
        if available <= 0:
            continue

        take = min(available, remaining_to_place)
        allocations.append(Allocation(po_item=po_item, qty=take))
        remaining_to_place -= take

    if remaining_to_place > 0:
        allocations.append(Allocation(po_item=None, qty=remaining_to_place))

    return allocations


def _already_allocated_in_receipt(
    db: Session, receipt_id: int, po_item_id: int
) -> Decimal:
    """Ebben a bevételezésben erre a rendeléstételre már lefoglalt mennyiség."""
    stmt = select(ReceiptItem.qty).where(
        ReceiptItem.receipt_id == receipt_id,
        ReceiptItem.purchase_order_item_id == po_item_id,
    )
    return sum((Decimal(q) for q in db.scalars(stmt)), Decimal(0))


def apply_scan(
    db: Session, receipt: Receipt, product: Product, qty: Decimal
) -> list[ReceiptItem]:
    """Egy beolvasás feldolgozása: allokáció + receipt_item sorok létrehozása/növelése.

    Ha ugyanarra a rendeléstételre már van sor ebben a bevételezésben,
    azt növeljük, nem hozunk létre újat.
    """
    created_or_updated: list[ReceiptItem] = []

    for alloc in allocate(db, receipt, product, qty):
        po_item = alloc.po_item
        existing = db.scalar(
            select(ReceiptItem).where(
                ReceiptItem.receipt_id == receipt.id,
                ReceiptItem.product_id == product.id,
                ReceiptItem.purchase_order_item_id
                == (po_item.id if po_item else None),
            )
        )

        if existing is not None:
            existing.qty = Decimal(existing.qty) + alloc.qty
            created_or_updated.append(existing)
            continue

        item = ReceiptItem(
            receipt_id=receipt.id,
            product_id=product.id,
            purchase_order_item_id=po_item.id if po_item else None,
            source=(
                ReceiptItemSource.from_order
                if po_item
                else ReceiptItemSource.outside_order
            ),
            qty=alloc.qty,
            net_unit_price=po_item.net_unit_price if po_item else None,
            sku_snapshot=product.sku,
            ean_snapshot=product.ean,
            name_snapshot=product.name,
            unit=product.unit,
            vat_name=product.vat_name or product.vat_rate,
        )
        db.add(item)
        created_or_updated.append(item)

    db.flush()
    return created_or_updated


def commit_receipt_to_orders(db: Session, receipt: Receipt) -> None:
    """Exportkor: a bevételezett mennyiségek rákönyvelése a rendelésekre.

    EZ az egyetlen hely, ahol a `received_qty` nő. Ezután a rendelések
    státusza újraszámolódik.
    """
    touched_orders: set[int] = set()

    for item in receipt.items:
        if item.purchase_order_item_id is None:
            continue
        po_item = db.get(PurchaseOrderItem, item.purchase_order_item_id)
        if po_item is None:
            continue
        po_item.received_qty = Decimal(po_item.received_qty) + Decimal(item.qty)
        touched_orders.add(po_item.purchase_order_id)

    for order_id in touched_orders:
        refresh_order_status(db, order_id)

    db.flush()


def refresh_order_status(db: Session, order_id: int) -> None:
    """Rendelés státusz újraszámolása a tételek alapján.

    Kézzel lezárt rendelést nem nyitunk vissza automatikusan.
    """
    order = db.get(PurchaseOrder, order_id)
    if order is None or order.closed_manually:
        return

    total_received = sum(Decimal(i.received_qty) for i in order.items)
    all_complete = all(i.remaining_qty <= 0 for i in order.items)

    if all_complete:
        order.status = PurchaseOrderStatus.closed
    elif total_received > 0:
        order.status = PurchaseOrderStatus.partial
    else:
        order.status = PurchaseOrderStatus.open
