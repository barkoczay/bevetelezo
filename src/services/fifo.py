"""FIFO allokáció: a beolvasott mennyiség szétosztása a nyitott rendelések között.

A raktáros ebből semmit nem lát és nem is dönt — beolvas, megad egy
mennyiséget, a rendszer eldönti, melyik rendelésre könyveli.

Sorrend: `order_date`, majd azonos dátumnál `order_number`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
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


def recalculate(db: Session, po_item_ids: Iterable[int]) -> None:
    """A rendeléstételek bevételezett mennyiségének újraszámolása.

    A `received_qty` MINDEN beolvasást tartalmaz, a bevételezés
    állapotától függetlenül — a beolvasott áru fizikailag megérkezett,
    ezért azonnal levonódik a rendelésből.

    Bármilyen változás után hívni kell: beolvasás, mennyiség módosítás,
    tétel törlése, bevételezés törlése.
    """
    order_ids: set[int] = set()

    for po_item_id in {i for i in po_item_ids if i is not None}:
        po_item = db.get(PurchaseOrderItem, po_item_id)
        if po_item is None:
            continue
        total = db.scalar(
            select(func.coalesce(func.sum(ReceiptItem.qty), 0)).where(
                ReceiptItem.purchase_order_item_id == po_item_id
            )
        )
        po_item.received_qty = Decimal(total or 0)
        order_ids.add(po_item.purchase_order_id)

    db.flush()
    for order_id in order_ids:
        refresh_order_status(db, order_id)
    db.flush()


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

        # A remaining_qty már minden beolvasást tartalmaz (a sajátunkat is),
        # ezért nincs szükség külön foglalás-számításra.
        available = po_item.remaining_qty
        if available <= 0:
            continue

        take = min(available, remaining_to_place)
        allocations.append(Allocation(po_item=po_item, qty=take))
        remaining_to_place -= take

    if remaining_to_place > 0:
        allocations.append(Allocation(po_item=None, qty=remaining_to_place))

    return allocations


def last_known_price(db: Session, product_id: int) -> Decimal | None:
    """A termék legutóbbi ismert nettó beszerzési ára a megrendelésekből.

    Rendelésen kívüli tételnél nincs honnan örökölni az árat, de a
    raktárostól nem kérünk árat — ezért a legutóbbi rendelés árát
    ajánljuk fel. Az admin ezt felülírhatja az export előtt.
    """
    stmt = (
        select(PurchaseOrderItem.net_unit_price)
        .join(PurchaseOrder)
        .where(
            PurchaseOrderItem.product_id == product_id,
            PurchaseOrderItem.net_unit_price.is_not(None),
        )
        .order_by(PurchaseOrder.order_date.desc(), PurchaseOrder.id.desc())
        .limit(1)
    )
    value = db.scalar(stmt)
    return Decimal(value) if value is not None else None


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

        price = (
            po_item.net_unit_price
            if po_item is not None
            else last_known_price(db, product.id)
        )

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
            net_unit_price=price,
            sku_snapshot=product.sku,
            ean_snapshot=product.ean,
            name_snapshot=product.name,
            unit=product.unit,
            vat_name=product.vat_name or product.vat_rate,
        )
        db.add(item)
        created_or_updated.append(item)

    db.flush()
    recalculate(db, [i.purchase_order_item_id for i in created_or_updated])
    return created_or_updated


def commit_receipt_to_orders(db: Session, receipt: Receipt) -> None:
    """Exportkor: biztonsági újraszámolás.

    A mennyiségek már a beolvasáskor rákerültek a rendelésre, itt csak
    megbizonyosodunk róla, hogy az összegek stimmelnek.
    """
    recalculate(db, [i.purchase_order_item_id for i in receipt.items])


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
