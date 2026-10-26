from __future__ import annotations

import operator
from typing import Annotated

from typing_extensions import TypedDict

from .models import (
    ApprovalDecision,
    Critique,
    ExtractionResult,
    FinalStatus,
    Invoice,
    NormalizationEvent,
    PaymentResult,
    ProcessResult,
    TraceEvent,
    ValidationResult,
)


class InvoiceState(TypedDict, total=False):
    run_id: str
    source_path: str
    source_files: list[str]
    source_hash: str
    source_hashes: list[str]
    source_format: str
    raw_text: str
    requires_extraction: bool
    invoice: Invoice
    extraction: ExtractionResult
    extraction_attempts: int
    repair_feedback: str
    canonical_errors: list[str]
    normalizations: list[NormalizationEvent]
    validation: ValidationResult
    approval: ApprovalDecision
    critique: Critique
    approval_revision_count: int
    critique_required: bool
    evidence_valid: bool
    approval_failure_reason: str
    final_status: FinalStatus
    payment: PaymentResult
    llm_mode: str
    replayed: bool
    replay_origin: str
    defer_payment: bool
    defer_audit: bool
    result: ProcessResult
    trace: Annotated[list[TraceEvent], operator.add]
