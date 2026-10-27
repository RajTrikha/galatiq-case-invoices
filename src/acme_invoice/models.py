"""Pydantic domain models and the evidence vocabulary shared across the workflow.

These types are the boundary between untrusted document input and validated state, so money is
Decimal throughout and never float.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FinalStatus(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class EvidenceStatus(StrEnum):
    PASSED = "passed"
    WARNING = "warning"
    BLOCKING = "blocking"


class Recommendation(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    HOLD = "hold"


class CritiqueVerdict(StrEnum):
    ACCEPT = "accept"
    REVISE = "revise"


class PaymentStatus(StrEnum):
    PAID = "paid"
    NOT_PAID = "not_paid"
    ALREADY_PAID = "already_paid"
    FAILED = "failed"


def parse_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | float):
        return Decimal(str(value))
    cleaned = str(value).strip().replace("$", "").replace("€", "").replace(",", "")
    # Scanned invoices carry the letter O where a zero belongs, as in INV-1012's "$3,500.O0".
    # Substituting unconditionally is safe because a letter is never valid in a decimal anyway:
    # the only alternative outcome is the ValueError raised below.
    cleaned = cleaned.replace("O", "0").replace("o", "0")
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid decimal value: {value!r}") from exc


class LineItem(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    description: str
    sku: str | None = None
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal | None = None
    note: str | None = None

    @field_validator("quantity", "unit_price", "amount", mode="before")
    @classmethod
    def _decimal_fields(cls, value: Any) -> Decimal | None:
        return parse_decimal(value)


class Invoice(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    invoice_number: str | None = None
    revision: str | None = None
    vendor_name: str | None = None
    invoice_date: date | None = None
    due_date: date | None = None
    currency: str | None = "USD"
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: Decimal | None = None
    tax: Decimal | None = None
    freight: Decimal | None = None
    total: Decimal | None = None
    payment_terms: str | None = None
    notes: str | None = None
    source_format: str | None = None

    @field_validator("subtotal", "tax", "freight", "total", mode="before")
    @classmethod
    def _money_fields(cls, value: Any) -> Decimal | None:
        return parse_decimal(value)

    @field_validator("currency")
    @classmethod
    def _currency_upper(cls, value: str | None) -> str | None:
        return value.upper() if value else value


class NormalizationEvent(BaseModel):
    field: str
    raw: str
    normalized: str
    method: str


class ExtractionResult(BaseModel):
    invoice: Invoice | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    unresolved_fields: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    attempts: int = 1
    origin: str = "deterministic"


class EvidenceRecord(BaseModel):
    id: str
    code: str
    status: EvidenceStatus
    field: str | None = None
    message: str
    expected: str | None = None
    actual: str | None = None
    required_outcome: FinalStatus | None = None


class ValidationResult(BaseModel):
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    aggregated_quantities: dict[str, Decimal] = Field(default_factory=dict)
    route: Literal["reject", "review", "viable"] = "viable"
    has_soft_warnings: bool = False

    @property
    def evidence_ids(self) -> set[str]:
        return {item.id for item in self.evidence}


class ToolCallRecord(BaseModel):
    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]


class ApprovalDecision(BaseModel):
    recommendation: Recommendation
    rationale: str
    evidence_ids: list[str] = Field(default_factory=list)
    risk_factors: list[str] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)


class Critique(BaseModel):
    verdict: CritiqueVerdict
    unsupported_evidence_ids: list[str] = Field(default_factory=list)
    missed_evidence_ids: list[str] = Field(default_factory=list)
    policy_conflicts: list[str] = Field(default_factory=list)
    feedback: str | None = None


class PaymentResult(BaseModel):
    status: PaymentStatus = PaymentStatus.NOT_PAID
    reference: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    invoice_id: str | None = None
    message: str | None = None

    @field_validator("amount", mode="before")
    @classmethod
    def _amount_decimal(cls, value: Any) -> Decimal | None:
        return parse_decimal(value)


class TraceEvent(BaseModel):
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    component: str
    kind: Literal["control", "agent", "tool", "action"]
    status: Literal["started", "completed", "warning", "failed"] = "completed"
    summary: str
    duration_ms: int | None = None


class ProcessResult(BaseModel):
    run_id: str
    source_files: list[str]
    source_hashes: list[str]
    llm_mode: Literal["live", "replay", "auto"]
    replayed: bool = False
    replay_origin: str | None = None
    invoice: Invoice | None = None
    normalizations: list[NormalizationEvent] = Field(default_factory=list)
    validation: ValidationResult
    approval: ApprovalDecision | None = None
    critique: Critique | None = None
    final_status: FinalStatus
    payment: PaymentResult
    trace: list[TraceEvent] = Field(default_factory=list)


class SourceProcessingError(BaseModel):
    source_file: str
    error_type: str
    message: str


class BatchProcessResult(BaseModel):
    results: list[ProcessResult] = Field(default_factory=list)
    errors: list[SourceProcessingError] = Field(default_factory=list)
