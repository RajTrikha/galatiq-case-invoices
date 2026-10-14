from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from acme_invoice.db import InvoiceRepository
from acme_invoice.models import ExtractionResult, Invoice, LineItem

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INVOICE_DIR = PROJECT_ROOT / "data" / "invoices"


@pytest.fixture
def invoice_dir() -> Path:
    return INVOICE_DIR


@pytest.fixture
def repository(tmp_path: Path) -> InvoiceRepository:
    return InvoiceRepository(tmp_path / "inventory.db", tmp_path / "ledger.db")


@pytest.fixture
def clean_invoice() -> Invoice:
    return Invoice(
        invoice_number="INV-TEST-1",
        vendor_name="Test Vendor Inc.",
        invoice_date=date(2026, 1, 1),
        due_date=date(2026, 1, 31),
        currency="USD",
        line_items=[
            LineItem(
                description="Widget A",
                sku="WidgetA",
                quantity=Decimal("2"),
                unit_price=Decimal("10.00"),
                amount=Decimal("20.00"),
            )
        ],
        subtotal=Decimal("20.00"),
        tax=Decimal("0"),
        total=Decimal("20.00"),
        payment_terms="Net 30",
        source_format="json",
    )


@pytest.fixture
def confident_extraction(clean_invoice: Invoice) -> ExtractionResult:
    return ExtractionResult(invoice=clean_invoice, confidence=0.99, origin="fake_test")
