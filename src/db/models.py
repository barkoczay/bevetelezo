from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# A BigInteger elsődleges kulcs SQLite-on nem autoincrementál (a teszteknél
# ez fontos), Postgresen viszont BIGSERIAL kell.
BigIntPK = BigInteger().with_variant(Integer, "sqlite")


class PurchaseOrderStatus(str, enum.Enum):
    open = "open"
    partial = "partial"
    closed = "closed"


class ReceiptStatus(str, enum.Enum):
    in_progress = "in_progress"
    scanned = "scanned"
    exported = "exported"


class ReceiptItemSource(str, enum.Enum):
    from_order = "from_order"
    outside_order = "outside_order"


class ProductSource(str, enum.Enum):
    """Honnan került be a termék a törzsbe."""

    naturasoft = "naturasoft"   # induló Naturasoft terméktörzs import
    unas = "unas"               # napi Unas szinkron
    order = "order"             # szállítói megrendelés feltöltéséből


# ---------------------------------------------------------------- felhasználó


class AppUser(Base):
    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    username: Mapped[str] = mapped_column(Text, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ---------------------------------------------------------------- szállító


class Supplier(Base):
    __tablename__ = "supplier"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ---------------------------------------------------------------- terméktörzs


class Product(Base):
    __tablename__ = "product"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    # A Naturasoft "Sorszám" / "Termék sorszám" oszlopa. Csak tájékoztató:
    # a párosítás CIKKSZÁM alapján történik, mert a Naturasoft bevételezés
    # importja is azon azonosít.
    naturasoft_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    ean: Mapped[str | None] = mapped_column(Text, index=True)
    # A termék azonosítója. Ez a párosítás kulcsa mindenhol.
    sku: Mapped[str] = mapped_column(Text, index=True)
    name: Mapped[str] = mapped_column(Text)
    manufacturer: Mapped[str | None] = mapped_column(Text)
    unit: Mapped[str] = mapped_column(Text, default="db")
    vat_rate: Mapped[str | None] = mapped_column(Text)
    vat_name: Mapped[str | None] = mapped_column(Text)
    weight_kg: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    product_group: Mapped[str | None] = mapped_column(Text)
    inactive: Mapped[bool] = mapped_column(Boolean, default=False)
    # Igaz, ha a termék biztosan létezik a Naturasoftban: vagy szerepelt az
    # induló Naturasoft importban, vagy volt már szállítói megrendelésen
    # (rendelésre csak Naturasoftban létező termék kerülhet).
    # Ha hamis, a bevételezés importja el fogja utasítani a sort — ezért az
    # admin figyelmeztetést kap. A raktáros ebből semmit nem lát.
    in_naturasoft: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[ProductSource] = mapped_column(
        Enum(ProductSource, name="product_source"), default=ProductSource.naturasoft
    )
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ---------------------------------------------------------------- megrendelés


class PurchaseOrder(Base):
    __tablename__ = "purchase_order"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    order_number: Mapped[str] = mapped_column(Text, unique=True)
    order_date: Mapped[date] = mapped_column(Date)  # FIFO alap
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("supplier.id"))
    warehouse: Mapped[str | None] = mapped_column(Text)
    status: Mapped[PurchaseOrderStatus] = mapped_column(
        Enum(PurchaseOrderStatus, name="purchase_order_status"),
        default=PurchaseOrderStatus.open,
    )
    source_filename: Mapped[str | None] = mapped_column(Text)
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_manually: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str | None] = mapped_column(Text)

    supplier: Mapped[Supplier | None] = relationship(lazy="joined")
    items: Mapped[list[PurchaseOrderItem]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )

    # Csak a feltöltés válaszához, nem tárolt mező.
    import_warnings: list[str] = []


class PurchaseOrderItem(Base):
    __tablename__ = "purchase_order_item"
    __table_args__ = (
        UniqueConstraint("purchase_order_id", "naturasoft_id", name="uq_po_item"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    purchase_order_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_order.id", ondelete="CASCADE")
    )
    product_id: Mapped[int | None] = mapped_column(ForeignKey("product.id"), index=True)
    naturasoft_id: Mapped[int] = mapped_column(BigInteger)
    sku_snapshot: Mapped[str] = mapped_column(Text)
    ean_snapshot: Mapped[str | None] = mapped_column(Text)
    name_snapshot: Mapped[str] = mapped_column(Text)
    unit: Mapped[str] = mapped_column(Text, default="db")
    ordered_qty: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    # Csak EXPORTÁLT bevételezésekből nő. A raktáros beolvasása nem érinti.
    received_qty: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0)
    net_unit_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    vat_rate: Mapped[str | None] = mapped_column(Text)
    line_no: Mapped[int] = mapped_column(Integer)

    order: Mapped[PurchaseOrder] = relationship(back_populates="items")
    product: Mapped[Product | None] = relationship(lazy="joined")

    @property
    def remaining_qty(self) -> Decimal:
        return Decimal(self.ordered_qty) - Decimal(self.received_qty)


# ---------------------------------------------------------------- bevételezés


class Receipt(Base):
    __tablename__ = "receipt"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("supplier.id"))
    status: Mapped[ReceiptStatus] = mapped_column(
        Enum(ReceiptStatus, name="receipt_status"), default=ReceiptStatus.in_progress
    )
    created_by: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    locked_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    delivery_note_no: Mapped[str | None] = mapped_column(Text)
    # Hivatkozási szám: a megrendelés száma, ami a Naturasoft bevételezés
    # fejlécébe kerül. Automatikusan az érintett rendelésekből töltődik,
    # de az admin felülírhatja (több rendelésnél vesszővel elválasztva).
    reference_number: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exported_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    export_filename: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)

    supplier: Mapped[Supplier] = relationship(lazy="joined")
    items: Mapped[list[ReceiptItem]] = relationship(
        back_populates="receipt", cascade="all, delete-orphan"
    )
    unknown_scans: Mapped[list[UnknownScan]] = relationship(
        back_populates="receipt", cascade="all, delete-orphan"
    )

    @property
    def editable(self) -> bool:
        """Export után zárolt."""
        return self.status != ReceiptStatus.exported

    @property
    def touched_order_numbers(self) -> list[str]:
        """Az érintett megrendelések száma, FIFO sorrendben.

        Ebből javasoljuk a hivatkozási számot, ha az admin még nem adott meg
        sajátot.
        """
        numbers: list[str] = []
        for item in self.items:
            if item.po_item is None or item.po_item.order is None:
                continue
            number = item.po_item.order.order_number
            if number not in numbers:
                numbers.append(number)
        return numbers

    @property
    def suggested_reference(self) -> str | None:
        numbers = self.touched_order_numbers
        return ", ".join(numbers) if numbers else None

    @property
    def effective_reference(self) -> str | None:
        """A ténylegesen használandó hivatkozási szám."""
        return self.reference_number or self.suggested_reference


class ReceiptItem(Base):
    __tablename__ = "receipt_item"
    __table_args__ = (CheckConstraint("qty > 0", name="ck_receipt_item_qty"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    receipt_id: Mapped[int] = mapped_column(
        ForeignKey("receipt.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"))
    purchase_order_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("purchase_order_item.id"), index=True
    )
    source: Mapped[ReceiptItemSource] = mapped_column(
        Enum(ReceiptItemSource, name="receipt_item_source")
    )
    qty: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    net_unit_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    sku_snapshot: Mapped[str] = mapped_column(Text)
    ean_snapshot: Mapped[str | None] = mapped_column(Text)
    name_snapshot: Mapped[str] = mapped_column(Text)
    unit: Mapped[str] = mapped_column(Text, default="db")
    vat_name: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    receipt: Mapped[Receipt] = relationship(back_populates="items")
    product: Mapped[Product] = relationship(lazy="joined")
    po_item: Mapped[PurchaseOrderItem | None] = relationship(lazy="joined")


class UnknownScan(Base):
    """Félretett beolvasás: sem a rendelésekben, sem a terméktörzsben nincs meg."""

    __tablename__ = "unknown_scan"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    receipt_id: Mapped[int] = mapped_column(
        ForeignKey("receipt.id", ondelete="CASCADE"), index=True
    )
    raw_code: Mapped[str] = mapped_column(Text)
    scanned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)

    receipt: Mapped[Receipt] = relationship(back_populates="unknown_scans")


class ProductImportLog(Base):
    __tablename__ = "product_import_log"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    filename: Mapped[str | None] = mapped_column(Text)
    rows_total: Mapped[int | None] = mapped_column(Integer)
    rows_created: Mapped[int | None] = mapped_column(Integer)
    rows_updated: Mapped[int | None] = mapped_column(Integer)
    rows_skipped: Mapped[int | None] = mapped_column(Integer)
    # JSONB Postgresen; SQLite-on (teszt) sima JSON
    warnings: Mapped[dict | None] = mapped_column(
        JSONB().with_variant(JSON, "sqlite")
    )
    imported_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
