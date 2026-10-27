"""Deterministic policy: the evidence records that decide how an invoice is routed.

This module, not an agent, is the authority on financial truth.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from .canonical import semantic_fingerprint
from .db import InvoiceRepository
from .models import (
    EvidenceRecord,
    EvidenceStatus,
    ExtractionResult,
    FinalStatus,
    Invoice,
    ValidationResult,
)

TOLERANCE = Decimal("0.01")
DUE_DATE_TOLERANCE_DAYS = 3
GROUNDED_SOURCE_FORMATS = {"txt", "pdf"}
_NUMERIC_LITERAL = re.compile(r"\d+(?:\.\d+)?")
_OCR_ZERO = re.compile(r"(?<=\d)[Oo](?=\d)|(?<=\d)[Oo]\b|\b[Oo](?=\d)")


class EvidenceBuilder:
    def __init__(self) -> None:
        self.items: list[EvidenceRecord] = []

    def add(
        self,
        code: str,
        status: EvidenceStatus,
        message: str,
        *,
        field: str | None = None,
        expected: object | None = None,
        actual: object | None = None,
        required_outcome: FinalStatus | None = None,
    ) -> EvidenceRecord:
        record = EvidenceRecord(
            id=f"EVID-{len(self.items) + 1:03d}",
            code=code,
            status=status,
            field=field,
            message=message,
            expected=None if expected is None else str(expected),
            actual=None if actual is None else str(actual),
            required_outcome=required_outcome,
        )
        self.items.append(record)
        return record


def validate_invoice(
    invoice: Invoice,
    extraction: ExtractionResult,
    raw_text: str,
    repository: InvoiceRepository,
) -> ValidationResult:
    evidence = EvidenceBuilder()
    _validate_extraction(extraction, evidence)
    extraction_uncertain = bool(extraction.unresolved_fields) or extraction.confidence < 0.75
    _validate_required_fields(invoice, evidence, extraction_uncertain=extraction_uncertain)
    computed_subtotal = _validate_arithmetic(invoice, evidence)
    aggregated = _validate_inventory(invoice, repository, evidence)
    _validate_currency(invoice, evidence)
    _validate_prior_payment(invoice, repository, evidence)
    _validate_revision_history(invoice, repository, evidence)
    _validate_soft_warnings(invoice, raw_text, evidence)
    _validate_total_threshold(invoice, evidence)
    # Grounding runs last so its records are appended after every other check. Curated replay
    # fixtures cite evidence by EVID id, and EvidenceBuilder numbers sequentially, so inserting
    # a check earlier would renumber the ids those fixtures reference.
    _validate_source_grounding(invoice, raw_text, evidence)

    required_outcomes = {
        item.required_outcome
        for item in evidence.items
        if item.status == EvidenceStatus.BLOCKING and item.required_outcome is not None
    }
    if FinalStatus.REJECTED in required_outcomes:
        route = "reject"
    elif FinalStatus.NEEDS_REVIEW in required_outcomes:
        route = "review"
    else:
        route = "viable"
    has_soft_warnings = any(item.status == EvidenceStatus.WARNING for item in evidence.items)
    if computed_subtotal is not None and invoice.subtotal is None:
        evidence.add(
            "VAL_SUBTOTAL_DERIVED",
            EvidenceStatus.PASSED,
            "Subtotal was derived from the validated line items.",
            field="subtotal",
            actual=f"{computed_subtotal:.2f}",
        )
    return ValidationResult(
        evidence=evidence.items,
        aggregated_quantities=dict(aggregated),
        route=route,
        has_soft_warnings=has_soft_warnings,
    )


def _validate_extraction(extraction: ExtractionResult, evidence: EvidenceBuilder) -> None:
    if extraction.unresolved_fields or extraction.confidence < 0.75:
        evidence.add(
            "VAL_LOW_EXTRACTION_CONFIDENCE",
            EvidenceStatus.BLOCKING,
            "The source could not be extracted with enough confidence for payment.",
            field=",".join(extraction.unresolved_fields) or None,
            expected="confidence >= 0.75 with no unresolved fields",
            actual=f"confidence={extraction.confidence:.2f}",
            required_outcome=FinalStatus.NEEDS_REVIEW,
        )
    else:
        evidence.add(
            "VAL_EXTRACTION_CONFIDENT",
            EvidenceStatus.PASSED,
            "The invoice was extracted with sufficient confidence.",
            expected="confidence >= 0.75",
            actual=f"{extraction.confidence:.2f}",
        )


def _validate_required_fields(
    invoice: Invoice, evidence: EvidenceBuilder, *, extraction_uncertain: bool
) -> None:
    required = [
        ("vendor_name", invoice.vendor_name, "VAL_MISSING_VENDOR"),
        ("invoice_number", invoice.invoice_number, "VAL_MISSING_INVOICE_NUMBER"),
        ("invoice_date", invoice.invoice_date, "VAL_MISSING_DATE"),
        ("due_date", invoice.due_date, "VAL_MISSING_DUE_DATE"),
        ("line_items", invoice.line_items, "VAL_MISSING_ITEMS"),
        ("total", invoice.total, "VAL_MISSING_TOTAL"),
    ]
    missing = False
    for field, value, code in required:
        if value is None or value == "" or value == []:
            missing = True
            outcome = FinalStatus.NEEDS_REVIEW if extraction_uncertain else FinalStatus.REJECTED
            evidence.add(
                code,
                EvidenceStatus.BLOCKING,
                (
                    f"Required field {field} could not be confirmed during extraction."
                    if extraction_uncertain
                    else f"Required field {field} is absent from the source."
                ),
                field=field,
                expected="present",
                actual="missing",
                required_outcome=outcome,
            )
    if not missing:
        evidence.add(
            "VAL_REQUIRED_FIELDS_PRESENT",
            EvidenceStatus.PASSED,
            "All mandatory invoice fields are present.",
        )


def _validate_arithmetic(invoice: Invoice, evidence: EvidenceBuilder) -> Decimal | None:
    if not invoice.line_items:
        return None
    subtotal = Decimal("0")
    arithmetic_valid = True
    for index, item in enumerate(invoice.line_items):
        if item.quantity <= 0:
            arithmetic_valid = False
            evidence.add(
                "VAL_NEGATIVE_QTY",
                EvidenceStatus.BLOCKING,
                "Invoice quantities must be greater than zero.",
                field=f"line_items[{index}].quantity",
                expected="> 0",
                actual=item.quantity,
                required_outcome=FinalStatus.REJECTED,
            )
        if item.unit_price < 0:
            arithmetic_valid = False
            evidence.add(
                "VAL_INVALID_PRICE",
                EvidenceStatus.BLOCKING,
                "Unit prices cannot be negative.",
                field=f"line_items[{index}].unit_price",
                expected=">= 0",
                actual=item.unit_price,
                required_outcome=FinalStatus.REJECTED,
            )
        expected_amount = item.quantity * item.unit_price
        subtotal += expected_amount
        if item.amount is not None and abs(item.amount - expected_amount) > TOLERANCE:
            arithmetic_valid = False
            evidence.add(
                "VAL_LINE_TOTAL_MISMATCH",
                EvidenceStatus.BLOCKING,
                "A line amount does not equal quantity multiplied by unit price.",
                field=f"line_items[{index}].amount",
                expected=f"{expected_amount:.2f}",
                actual=f"{item.amount:.2f}",
                required_outcome=FinalStatus.REJECTED,
            )
    if invoice.subtotal is not None and abs(invoice.subtotal - subtotal) > TOLERANCE:
        arithmetic_valid = False
        evidence.add(
            "VAL_TOTAL_MISMATCH",
            EvidenceStatus.BLOCKING,
            "Printed subtotal does not equal the sum of line items.",
            field="subtotal",
            expected=f"{subtotal:.2f}",
            actual=f"{invoice.subtotal:.2f}",
            required_outcome=FinalStatus.REJECTED,
        )
    base = invoice.subtotal if invoice.subtotal is not None else subtotal
    expected_total = base + (invoice.tax or Decimal("0")) + (invoice.freight or Decimal("0"))
    if invoice.total is not None and abs(invoice.total - expected_total) > TOLERANCE:
        arithmetic_valid = False
        evidence.add(
            "VAL_TOTAL_MISMATCH",
            EvidenceStatus.BLOCKING,
            "Printed total does not reconcile with subtotal, tax, and freight.",
            field="total",
            expected=f"{expected_total:.2f}",
            actual=f"{invoice.total:.2f}",
            required_outcome=FinalStatus.REJECTED,
        )
    if arithmetic_valid and invoice.total is not None:
        evidence.add(
            "VAL_TOTAL_RECONCILED",
            EvidenceStatus.PASSED,
            "Line items and printed totals reconcile within one cent.",
            field="total",
            expected=f"{expected_total:.2f}",
            actual=f"{invoice.total:.2f}",
        )
    return subtotal


def _validate_inventory(
    invoice: Invoice, repository: InvoiceRepository, evidence: EvidenceBuilder
) -> defaultdict[str, Decimal]:
    aggregated: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for index, item in enumerate(invoice.line_items):
        if not item.sku:
            evidence.add(
                "VAL_UNKNOWN_SKU",
                EvidenceStatus.BLOCKING,
                f"Item {item.description!r} could not be matched conservatively to inventory.",
                field=f"line_items[{index}].sku",
                expected="known catalog item",
                actual=item.description,
                required_outcome=FinalStatus.NEEDS_REVIEW,
            )
            continue
        aggregated[item.sku] += item.quantity
    for sku, requested in sorted(aggregated.items()):
        snapshot = repository.lookup_inventory(sku)
        if not snapshot["found"]:
            evidence.add(
                "VAL_UNKNOWN_SKU",
                EvidenceStatus.BLOCKING,
                f"Item {sku!r} is not present in inventory.",
                field="line_items.sku",
                expected="known catalog item",
                actual=sku,
                required_outcome=FinalStatus.NEEDS_REVIEW,
            )
            continue
        available = int(snapshot["stock"])
        if available == 0:
            evidence.add(
                "VAL_ZERO_STOCK",
                EvidenceStatus.BLOCKING,
                f"Known item {sku} has zero available stock.",
                field="line_items.quantity",
                expected="available stock > 0",
                actual=f"requested={requested}, available=0",
                required_outcome=FinalStatus.REJECTED,
            )
        elif requested > available:
            evidence.add(
                "VAL_INSUFFICIENT_STOCK",
                EvidenceStatus.BLOCKING,
                f"Requested quantity for {sku} exceeds available stock.",
                field="line_items.quantity",
                expected=f"<= {available}",
                actual=str(requested),
                required_outcome=FinalStatus.REJECTED,
            )
        else:
            evidence.add(
                "VAL_STOCK_SUFFICIENT",
                EvidenceStatus.PASSED,
                f"Available stock covers the aggregated quantity for {sku}.",
                field="line_items.quantity",
                expected=f"<= {available}",
                actual=str(requested),
            )
    return aggregated


def _validate_currency(invoice: Invoice, evidence: EvidenceBuilder) -> None:
    if invoice.currency == "USD":
        evidence.add(
            "VAL_CURRENCY_SUPPORTED",
            EvidenceStatus.PASSED,
            "USD is supported by the mock payment workflow.",
            field="currency",
            actual="USD",
        )
    else:
        evidence.add(
            "VAL_UNSUPPORTED_CURRENCY",
            EvidenceStatus.BLOCKING,
            "No verified FX rate is available for this invoice currency.",
            field="currency",
            expected="USD",
            actual=invoice.currency or "missing",
            required_outcome=FinalStatus.NEEDS_REVIEW,
        )


def _validate_prior_payment(
    invoice: Invoice, repository: InvoiceRepository, evidence: EvidenceBuilder
) -> None:
    prior = repository.payment_for_identity(invoice.vendor_name, invoice.invoice_number)
    if prior:
        evidence.add(
            "VAL_DUPLICATE_PAID",
            EvidenceStatus.BLOCKING,
            "This vendor and invoice number already have a recorded payment.",
            field="invoice_number",
            expected="unpaid invoice identity",
            actual=str(prior["reference"]),
            required_outcome=FinalStatus.NEEDS_REVIEW,
        )
    else:
        evidence.add(
            "VAL_NO_PRIOR_PAYMENT",
            EvidenceStatus.PASSED,
            "No prior payment exists for this vendor and invoice number.",
            field="invoice_number",
            actual=invoice.invoice_number,
        )


def _validate_revision_history(
    invoice: Invoice, repository: InvoiceRepository, evidence: EvidenceBuilder
) -> None:
    if invoice.revision:
        evidence.add(
            "VAL_REVISION_CONFLICT",
            EvidenceStatus.BLOCKING,
            "An explicitly revised invoice requires confirmation of the authoritative version.",
            field="revision",
            expected="base invoice or human-confirmed revision",
            actual=invoice.revision,
            required_outcome=FinalStatus.NEEDS_REVIEW,
        )
        return
    prior_fingerprints = repository.prior_invoice_fingerprints(
        invoice.vendor_name, invoice.invoice_number
    )
    if prior_fingerprints and semantic_fingerprint(invoice) not in prior_fingerprints:
        evidence.add(
            "VAL_REVISION_CONFLICT",
            EvidenceStatus.BLOCKING,
            "This invoice identity has materially different content in prior run history.",
            field="invoice_number",
            expected="same dates, items, quantities, prices, currency, and total",
            actual="material content changed",
            required_outcome=FinalStatus.NEEDS_REVIEW,
        )


def _validate_soft_warnings(invoice: Invoice, raw_text: str, evidence: EvidenceBuilder) -> None:
    lowered = raw_text.casefold()
    if "formerly" in lowered or "now part of" in lowered:
        evidence.add(
            "VAL_VENDOR_RENAME",
            EvidenceStatus.WARNING,
            "The document claims a vendor rename or former legal identity.",
            field="vendor_name",
            actual=invoice.vendor_name,
        )
    if invoice.invoice_date and invoice.due_date and invoice.payment_terms:
        match = re.search(r"net\s+(\d+)", invoice.payment_terms, flags=re.IGNORECASE)
        if match:
            expected_due = invoice.invoice_date + timedelta(days=int(match.group(1)))
            if abs((invoice.due_date - expected_due).days) > DUE_DATE_TOLERANCE_DAYS:
                evidence.add(
                    "VAL_DUE_DATE_ODDITY",
                    EvidenceStatus.WARNING,
                    "Printed due date differs materially from the stated payment terms.",
                    field="due_date",
                    expected=expected_due.isoformat(),
                    actual=invoice.due_date.isoformat(),
                )
    signal_patterns = {
        "urgent": r"\burgent(?:ly)?\b",
        "pay_immediately": r"pay immediately",
        "wire_transfer": r"wire transfer",
        "avoid_penalties": r"avoid penalties",
        "skip_review": r"skip review|pre-approved|process without review",
        "prompt_injection": r"ignore (?:all |the )?(?:previous|prior) instructions?",
    }
    signals = [name for name, pattern in signal_patterns.items() if re.search(pattern, lowered)]
    if len(signals) >= 2:
        evidence.add(
            "VAL_SUSPICIOUS_LANGUAGE",
            EvidenceStatus.BLOCKING,
            "Multiple payment-pressure signals require human fraud review.",
            field="notes",
            actual=", ".join(signals),
            required_outcome=FinalStatus.NEEDS_REVIEW,
        )
    elif signals:
        evidence.add(
            "VAL_SUSPICIOUS_LANGUAGE",
            EvidenceStatus.WARNING,
            "The document contains a payment-pressure signal for approval review.",
            field="notes",
            actual=signals[0],
        )


def _validate_total_threshold(invoice: Invoice, evidence: EvidenceBuilder) -> None:
    if invoice.total is not None and invoice.currency == "USD" and invoice.total > Decimal("10000"):
        evidence.add(
            "VAL_HIGH_VALUE",
            EvidenceStatus.WARNING,
            "Invoice exceeds the $10,000 enhanced-approval threshold.",
            field="total",
            expected="<= 10000.00 for standard approval",
            actual=f"{invoice.total:.2f}",
        )


def source_numeric_pool(raw_text: str) -> set[Decimal]:
    """Every numeric literal in the source, normalized for currency, grouping, and OCR zeros."""
    cleaned = raw_text.replace("$", "").replace("€", "").replace(",", "")
    return {
        Decimal(match.group()) for match in _NUMERIC_LITERAL.finditer(_OCR_ZERO.sub("0", cleaned))
    }


def _validate_source_grounding(invoice: Invoice, raw_text: str, evidence: EvidenceBuilder) -> None:
    """Confirm that agent-extracted figures were actually printed in the source document.

    Only TXT and PDF are checked; structured formats are parsed rather than interpreted, so
    their figures cannot be hallucinated. Derived line amounts are exempt because sources such
    as INV-1001 and INV-1008 print quantity and unit price without the product. A zero subtotal,
    tax, or freight is exempt because it asserts that a charge is absent rather than reporting a
    figure on the page; a zero total, quantity, or unit price is still required to be grounded.

    Comparison is numeric, never textual: INV-1002 prints "$750" and INV-1012 prints "$250",
    so a string search for "750.00" would reject valid extractions.
    """
    if invoice.source_format not in GROUNDED_SOURCE_FORMATS or not invoice.line_items:
        return
    pool = source_numeric_pool(raw_text)
    # (field, value, zero_means_absent): only an omitted charge may be zero without appearing.
    figures: list[tuple[str, Decimal, bool]] = []
    if invoice.total is not None:
        figures.append(("total", invoice.total, False))
    for field, value in (
        ("subtotal", invoice.subtotal),
        ("tax", invoice.tax),
        ("freight", invoice.freight),
    ):
        if value is not None:
            figures.append((field, value, True))
    for index, item in enumerate(invoice.line_items):
        figures.append((f"line_items[{index}].quantity", item.quantity, False))
        figures.append((f"line_items[{index}].unit_price", item.unit_price, False))

    unsupported = [
        (field, value)
        for field, value, zero_means_absent in figures
        if not (zero_means_absent and value == 0) and value not in pool
    ]
    if not unsupported:
        evidence.add(
            "VAL_SOURCE_FIGURES_GROUNDED",
            EvidenceStatus.PASSED,
            "Every payment-critical figure was located in the source document.",
            field="total",
            expected="each extracted figure printed in the source",
            actual=f"{len(figures)} figures matched",
        )
        return
    for field, value in unsupported:
        evidence.add(
            "VAL_SOURCE_FIGURE_UNSUPPORTED",
            EvidenceStatus.BLOCKING,
            "An extracted figure does not appear anywhere in the source document.",
            field=field,
            expected="a value printed in the source",
            actual=f"{value}",
            required_outcome=FinalStatus.NEEDS_REVIEW,
        )
