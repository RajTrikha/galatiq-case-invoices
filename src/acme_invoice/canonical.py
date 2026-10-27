"""Canonical forms used for comparison across formats and runs.

Content hashes, vendor and invoice identity, SKU matching, lenient dates, and the semantic
fingerprint that distinguishes a reprinted invoice from a revised one.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from dateutil import parser as date_parser

from .models import Invoice


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def normalize_invoice_number(value: str | None) -> str:
    if not value:
        return ""
    compact = re.sub(r"[^A-Z0-9]", "", value.upper())
    if compact.startswith("INVOICE"):
        compact = compact[7:]
    if compact.startswith("INV"):
        compact = compact[3:]
    # Leading zeros are dropped so a vendor that pads inconsistently across reprints still
    # resolves to one payment identity. The trade-off is that INV-0012 and INV-12 collide, which
    # is the safer direction to fail: a false duplicate is reviewed, a missed one is paid twice.
    return compact.lstrip("0") or "0"


def normalize_vendor(value: str | None) -> str:
    if not value:
        return ""
    lowered = value.casefold().replace("&", "and")
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    legal_suffixes = {
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "co",
        "company",
        "ltd",
        "limited",
    }
    tokens = [token for token in lowered.split() if token not in legal_suffixes]
    return "".join(tokens)


def compact_sku(value: str) -> str:
    without_qualifier = re.sub(r"\([^)]*\)", "", value)
    return re.sub(r"[^a-z0-9]", "", without_qualifier.casefold())


def parse_date_value(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    raw = str(value).strip()
    if raw.casefold() in {"yesterday", "today", "tomorrow"}:
        return None
    repaired = re.sub(r"(?<=\d)[Oo](?=\d)", "0", raw)
    try:
        return date_parser.parse(repaired, fuzzy=False).date()
    except (ValueError, OverflowError):
        return None


def decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.quantize(Decimal("0.01")), "f")


def semantic_fingerprint(invoice: Invoice) -> str:
    payload = {
        "invoice_number": normalize_invoice_number(invoice.invoice_number),
        "vendor": normalize_vendor(invoice.vendor_name),
        "invoice_date": invoice.invoice_date.isoformat() if invoice.invoice_date else None,
        "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
        "currency": invoice.currency,
        "items": sorted(
            [
                {
                    "sku": compact_sku(item.sku or item.description),
                    "quantity": str(item.quantity),
                    "unit_price": decimal_text(item.unit_price),
                    "amount": decimal_text(item.amount),
                }
                for item in invoice.line_items
            ],
            key=lambda item: (item["sku"], item["quantity"], item["unit_price"] or ""),
        ),
        "total": decimal_text(invoice.total),
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()
