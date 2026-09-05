from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------- auth


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    display_name: str


# ---------------------------------------------------------------- szállító


class SupplierIn(BaseModel):
    name: str = Field(min_length=1)
    active: bool = True


class SupplierOut(ORMModel):
    id: int
    name: str
    active: bool


# ---------------------------------------------------------------- termék


class ProductOut(ORMModel):
    id: int
    naturasoft_id: int
    ean: str | None
    sku: str
    name: str
    unit: str
    inactive: bool
    in_naturasoft: bool = True
    source: str = "naturasoft"


class ProductImportOut(BaseModel):
    rows_total: int
    created: int
    updated: int
    skipped: int
    warnings: list[str]


# ---------------------------------------------------------------- megrendelés


class OrderItemOut(ORMModel):
    id: int
    naturasoft_id: int
    sku_snapshot: str
    ean_snapshot: str | None
    name_snapshot: str
    unit: str
    ordered_qty: Decimal
    received_qty: Decimal
    remaining_qty: Decimal
    net_unit_price: Decimal | None
    line_no: int


class OrderOut(ORMModel):
    id: int
    order_number: str
    order_date: date
    supplier_id: int | None
    supplier_name: str | None = None
    warehouse: str | None
    status: str
    closed_manually: bool
    uploaded_at: datetime
    note: str | None
    # összesítők a listanézethez
    item_count: int = 0
    completed_item_count: int = 0
    ordered_total: Decimal = Decimal(0)
    received_total: Decimal = Decimal(0)


class OrderDetailOut(OrderOut):
    items: list[OrderItemOut] = []


class OrderPreviewOut(BaseModel):
    """A feltöltés előtti előnézet: mit talált a fájlban."""

    order_number: str | None
    warehouse: str | None
    item_count: int
    items: list[dict]
    warnings: list[str]
    already_exists: bool


class OrderUpdateIn(BaseModel):
    order_date: date | None = None
    supplier_id: int | None = None
    note: str | None = None


# ---------------------------------------------------------------- bevételezés


class ReceiptCreateIn(BaseModel):
    supplier_id: int
    delivery_note_no: str | None = None


class ScanIn(BaseModel):
    code: str = Field(min_length=1)
    qty: Decimal = Decimal(1)


class ScanOut(BaseModel):
    """A raktáros felületét ez vezérli."""

    status: str          # ok | unknown | inactive
    message: str | None = None
    product_name: str | None = None
    unit: str | None = None
    total_qty: Decimal | None = None   # a termék össz mennyisége ebben a bevételezésben
    item_ids: list[int] = []


class ReceiptItemOut(ORMModel):
    id: int
    product_id: int
    purchase_order_item_id: int | None
    source: str
    qty: Decimal
    net_unit_price: Decimal | None
    sku_snapshot: str
    ean_snapshot: str | None
    name_snapshot: str
    unit: str
    vat_name: str | None
    note: str | None
    order_number: str | None = None
    # Admin figyelmeztetés: a termék nincs a Naturasoftban, az import
    # elutasítaná ezt a sort. A raktáros ezt nem látja.
    missing_in_naturasoft: bool = False


class UnknownScanOut(ORMModel):
    id: int
    raw_code: str
    scanned_at: datetime
    resolved: bool


class ReceiptOut(ORMModel):
    id: int
    supplier_id: int
    supplier_name: str | None = None
    status: str
    reference_number: str | None
    suggested_reference: str | None = None
    delivery_note_no: str | None
    created_at: datetime
    scanned_at: datetime | None
    exported_at: datetime | None
    export_filename: str | None
    note: str | None
    item_count: int = 0
    unknown_count: int = 0
    # Hány tétel hiányzik a Naturasoftból (admin figyelmeztetés)
    missing_in_naturasoft_count: int = 0


class ReceiptDetailOut(ReceiptOut):
    items: list[ReceiptItemOut] = []
    unknown_scans: list[UnknownScanOut] = []


class ReceiptUpdateIn(BaseModel):
    reference_number: str | None = None
    delivery_note_no: str | None = None
    note: str | None = None


class ReceiptItemUpdateIn(BaseModel):
    qty: Decimal | None = Field(default=None, gt=0)
    net_unit_price: Decimal | None = None
    note: str | None = None
