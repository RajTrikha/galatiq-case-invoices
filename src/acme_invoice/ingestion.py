from __future__ import annotations

import csv
import io
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import Invoice, LineItem, NormalizationEvent
from .utils import compact_sku, parse_date_value, sha256_bytes

SUPPORTED_SUFFIXES = {".txt", ".pdf", ".json", ".csv", ".xml"}


@dataclass(frozen=True)
class LoadedDocument:
    path: Path
    source_hash: str
    source_format: str
    raw_text: str
    invoice: Invoice | None
    requires_extraction: bool


def load_document(path: Path) -> LoadedDocument:
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Invoice file not found: {path}")
    suffix = path.suffix.casefold()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(f"Unsupported invoice format: {suffix or '<none>'}")
    content = path.read_bytes()
    source_hash = sha256_bytes(content)

    if suffix == ".pdf":
        raw_text = _extract_pdf_text(path)
        return LoadedDocument(path, source_hash, "pdf", raw_text, None, True)
    raw_text = content.decode("utf-8", errors="replace")
    if suffix == ".txt":
        return LoadedDocument(path, source_hash, "txt", raw_text, None, True)
    if suffix == ".json":
        invoice = _parse_json(raw_text)
    elif suffix == ".csv":
        invoice = _parse_csv(raw_text)
    else:
        invoice = _parse_xml(raw_text)
    invoice.source_format = suffix.removeprefix(".")
    return LoadedDocument(path, source_hash, invoice.source_format, raw_text, invoice, False)


def _extract_pdf_text(path: Path) -> str:
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - exercised by installation contract
        raise RuntimeError("pdfplumber is required for PDF invoices") from exc
    with pdfplumber.open(path) as pdf:
        pages = [(page.extract_text(layout=True) or "").strip() for page in pdf.pages]
    return "\n\n".join(page for page in pages if page).strip()


def _parse_json(raw_text: str) -> Invoice:
    data = json.loads(raw_text)
    if not isinstance(data, dict):
        raise ValueError("JSON invoice must contain an object")
    return invoice_from_mapping(data)


def _first_present(data: dict[str, Any], *keys: str) -> Any:
    """Return the first explicitly populated value without discarding numeric zero."""
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
    return None


def invoice_from_mapping(data: dict[str, Any]) -> Invoice:
    vendor = data.get("vendor")
    vendor_name = vendor.get("name") if isinstance(vendor, dict) else vendor
    item_rows = data.get("line_items") or data.get("items") or []
    line_items = []
    for row in item_rows:
        if not isinstance(row, dict):
            continue
        description = row.get("description") or row.get("item") or row.get("name") or ""
        line_items.append(
            LineItem(
                description=str(description),
                sku=row.get("sku") or row.get("item"),
                quantity=row.get("quantity") if "quantity" in row else row.get("qty"),
                unit_price=row.get("unit_price") if "unit_price" in row else row.get("price"),
                amount=_first_present(row, "amount", "line_total"),
                note=row.get("note"),
            )
        )
    return Invoice(
        invoice_number=data.get("invoice_number") or data.get("invoice") or data.get("number"),
        revision=data.get("revision"),
        vendor_name=vendor_name or data.get("vendor_name"),
        invoice_date=parse_date_value(data.get("date") or data.get("invoice_date")),
        due_date=parse_date_value(data.get("due_date") or data.get("due")),
        currency=data.get("currency") or "USD",
        line_items=line_items,
        subtotal=data.get("subtotal"),
        tax=data.get("tax_amount") if "tax_amount" in data else data.get("tax"),
        freight=_first_present(data, "freight", "shipping"),
        total=_first_present(data, "total", "total_amount", "amount"),
        payment_terms=data.get("payment_terms") or data.get("terms"),
        notes=data.get("notes"),
    )


def _parse_csv(raw_text: str) -> Invoice:
    rows = list(csv.reader(io.StringIO(raw_text)))
    if not rows:
        raise ValueError("CSV invoice is empty")
    if [cell.strip().casefold() for cell in rows[0][:2]] == ["field", "value"]:
        return _parse_vertical_csv(rows[1:])
    return _parse_tabular_csv(raw_text)


def _parse_vertical_csv(rows: list[list[str]]) -> Invoice:
    scalar: dict[str, str] = {}
    items: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for row in rows:
        if len(row) < 2:
            continue
        key, value = row[0].strip().casefold(), row[1].strip()
        if key == "item":
            if current:
                items.append(current)
            current = {"item": value}
        elif key in {"quantity", "unit_price"} and current is not None:
            current[key] = value
        else:
            scalar[key] = value
    if current:
        items.append(current)
    return invoice_from_mapping(
        {
            "invoice_number": scalar.get("invoice_number"),
            "vendor": scalar.get("vendor"),
            "date": scalar.get("date"),
            "due_date": scalar.get("due_date"),
            "line_items": items,
            "subtotal": scalar.get("subtotal"),
            "tax": scalar.get("tax"),
            "total": scalar.get("total"),
            "payment_terms": scalar.get("payment_terms"),
            "currency": scalar.get("currency", "USD"),
        }
    )


def _parse_tabular_csv(raw_text: str) -> Invoice:
    reader = csv.DictReader(io.StringIO(raw_text))
    items: list[dict[str, str]] = []
    header: dict[str, str] = {}
    totals: dict[str, str] = {}
    for raw_row in reader:
        row = {(key or "").strip(): (value or "").strip() for key, value in raw_row.items()}
        item_name = row.get("Item", "")
        if item_name:
            if not header:
                header = row
            items.append(
                {
                    "item": item_name,
                    "quantity": row.get("Qty", ""),
                    "unit_price": row.get("Unit Price", ""),
                    "amount": row.get("Line Total", ""),
                }
            )
            continue
        label = row.get("Unit Price", "").casefold()
        value = row.get("Line Total", "")
        if "subtotal" in label:
            totals["subtotal"] = value
        elif "tax" in label:
            totals["tax"] = value
        elif "total" in label:
            totals["total"] = value
    return invoice_from_mapping(
        {
            "invoice_number": header.get("Invoice Number"),
            "vendor": header.get("Vendor"),
            "date": header.get("Date"),
            "due_date": header.get("Due Date"),
            "line_items": items,
            **totals,
            "currency": "USD",
        }
    )


def _parse_xml(raw_text: str) -> Invoice:
    root = ET.fromstring(raw_text)

    def text(path: str) -> str | None:
        node = root.find(path)
        return node.text.strip() if node is not None and node.text else None

    items: list[dict[str, str | None]] = []
    for node in root.findall("./line_items/item"):
        items.append(
            {
                "name": node.findtext("name"),
                "quantity": node.findtext("quantity"),
                "unit_price": node.findtext("unit_price"),
                "amount": node.findtext("amount"),
            }
        )
    return invoice_from_mapping(
        {
            "invoice_number": text("./header/invoice_number"),
            "vendor": text("./header/vendor"),
            "date": text("./header/date"),
            "due_date": text("./header/due_date"),
            "currency": text("./header/currency") or "USD",
            "line_items": items,
            "subtotal": text("./totals/subtotal"),
            "tax_amount": text("./totals/tax_amount"),
            "total": text("./totals/total"),
            "payment_terms": text("./payment_terms"),
        }
    )


def canonicalize_invoice(
    invoice: Invoice, inventory_items: list[str]
) -> tuple[Invoice, list[NormalizationEvent]]:
    events: list[NormalizationEvent] = []
    inventory_map = {compact_sku(item): item for item in inventory_items}
    normalized_items: list[LineItem] = []
    for index, item in enumerate(invoice.line_items):
        raw_sku = item.sku or item.description
        matched = inventory_map.get(compact_sku(raw_sku))
        if matched and matched != raw_sku:
            events.append(
                NormalizationEvent(
                    field=f"line_items[{index}].sku",
                    raw=raw_sku,
                    normalized=matched,
                    method="canonical_spacing_and_qualifier",
                )
            )
        normalized_items.append(item.model_copy(update={"sku": matched}))

    invoice_number = invoice.invoice_number
    if invoice_number:
        match = re.fullmatch(r"\s*INV\s+(\d+)\s*", invoice_number, flags=re.IGNORECASE)
        if match:
            normalized_number = f"INV-{match.group(1)}"
            events.append(
                NormalizationEvent(
                    field="invoice_number",
                    raw=invoice_number,
                    normalized=normalized_number,
                    method="canonical_invoice_separator",
                )
            )
            invoice_number = normalized_number
    normalized = invoice.model_copy(
        update={"invoice_number": invoice_number, "line_items": normalized_items}
    )
    return normalized, events
