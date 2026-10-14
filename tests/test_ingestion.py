from __future__ import annotations

from decimal import Decimal

import pytest

from acme_invoice.ingestion import canonicalize_invoice, load_document
from acme_invoice.models import Invoice, LineItem


@pytest.mark.parametrize(
    ("filename", "source_format", "requires_extraction"),
    [
        ("invoice_1004.json", "json", False),
        ("invoice_1006.csv", "csv", False),
        ("invoice_1015.csv", "csv", False),
        ("invoice_1014.xml", "xml", False),
        ("invoice_1001.txt", "txt", True),
        ("invoice_1011.pdf", "pdf", True),
    ],
)
def test_loads_every_supported_path(
    invoice_dir, filename: str, source_format: str, requires_extraction: bool
) -> None:
    loaded = load_document(invoice_dir / filename)
    assert loaded.source_format == source_format
    assert loaded.requires_extraction is requires_extraction
    assert loaded.raw_text
    assert len(loaded.source_hash) == 64


def test_both_csv_layouts_produce_canonical_candidates(invoice_dir) -> None:
    vertical = load_document(invoice_dir / "invoice_1006.csv").invoice
    tabular = load_document(invoice_dir / "invoice_1015.csv").invoice
    assert vertical and vertical.invoice_number == "INV-1006"
    assert tabular and tabular.invoice_number == "INV-1015"
    assert vertical.line_items and tabular.line_items


def test_pdf_text_is_extracted(invoice_dir) -> None:
    loaded = load_document(invoice_dir / "invoice_1012.pdf")
    assert "INV" in loaded.raw_text
    assert len(loaded.raw_text) > 100


def test_conservative_sku_normalization_records_changes() -> None:
    invoice = Invoice(
        line_items=[
            LineItem(description="Widget A", quantity=1, unit_price=1),
            LineItem(description="WidgetC", quantity=1, unit_price=1),
        ]
    )
    normalized, events = canonicalize_invoice(
        invoice, ["WidgetA", "WidgetB", "GadgetX", "FakeItem"]
    )
    assert normalized.line_items[0].sku == "WidgetA"
    assert normalized.line_items[1].sku is None
    assert [(event.raw, event.normalized) for event in events] == [("Widget A", "WidgetA")]


def test_repeated_skus_remain_distinct_lines_for_audit() -> None:
    invoice = Invoice(
        line_items=[
            LineItem(description="Widget A", sku="Widget A", quantity=1, unit_price=1),
            LineItem(description="WidgetA", sku="WidgetA", quantity=2, unit_price=1),
        ]
    )
    normalized, _ = canonicalize_invoice(invoice, ["WidgetA"])
    assert [item.sku for item in normalized.line_items] == ["WidgetA", "WidgetA"]
    assert sum(item.quantity for item in normalized.line_items) == Decimal("3")


def test_unsupported_file_type_is_rejected(tmp_path) -> None:
    path = tmp_path / "invoice.docx"
    path.write_text("not an invoice")
    with pytest.raises(ValueError, match="Unsupported invoice format"):
        load_document(path)
