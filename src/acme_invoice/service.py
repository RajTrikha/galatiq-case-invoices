from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from .db import InvoiceRepository
from .graph import InvoiceWorkflow
from .ingestion import SUPPORTED_SUFFIXES, DocumentParseError
from .llm import LLMClient, make_llm_client
from .models import (
    BatchProcessResult,
    EvidenceRecord,
    EvidenceStatus,
    FinalStatus,
    PaymentResult,
    PaymentStatus,
    ProcessResult,
    SourceProcessingError,
    TraceEvent,
)
from .utils import normalize_invoice_number, normalize_vendor, semantic_fingerprint, sha256_file


class InvoiceService:
    def __init__(
        self,
        *,
        llm_mode: str = "auto",
        runtime_dir: str | Path | None = None,
        inventory_path: str | Path | None = None,
        ledger_path: str | Path | None = None,
        replay_path: str | Path | None = None,
        llm_client: LLMClient | None = None,
    ) -> None:
        project_root = Path(__file__).resolve().parents[2]
        runtime = Path(runtime_dir) if runtime_dir is not None else project_root / ".runtime"
        self.repository = InvoiceRepository(
            Path(inventory_path) if inventory_path is not None else runtime / "inventory.db",
            Path(ledger_path) if ledger_path is not None else runtime / "ledger.db",
        )
        self.llm_mode = llm_mode
        replay = (
            Path(replay_path)
            if replay_path is not None
            else project_root / "data" / "replay" / "agent_responses.json"
        )
        llm = llm_client or make_llm_client(llm_mode, replay)
        self.workflow = InvoiceWorkflow(self.repository, llm, llm_mode)

    def process(self, source_path: str | Path) -> ProcessResult:
        path = Path(source_path).resolve()
        paid_result = self._exact_paid_result(path)
        if paid_result:
            self.repository.record_run(paid_result)
            return paid_result
        return self.workflow.invoke(path)

    def _exact_paid_result(self, path: Path) -> ProcessResult | None:
        source_hash = sha256_file(path)
        paid_result = self.repository.load_paid_result_for_source(source_hash)
        paid_record = self.repository.payment_for_source(source_hash)
        if paid_result and paid_record:
            return paid_result.model_copy(
                update={
                    "run_id": str(uuid.uuid4()),
                    "payment": PaymentResult(
                        status=PaymentStatus.ALREADY_PAID,
                        reference=str(paid_record["reference"]),
                        amount=paid_record["amount"],
                        currency=str(paid_record["currency"]),
                        invoice_id=(
                            paid_result.invoice.invoice_number if paid_result.invoice else None
                        ),
                        message="Exact paid source was recognized; no workflow or payment reran.",
                    ),
                    "trace": [
                        *paid_result.trace,
                        TraceEvent(
                            component="idempotency_guard",
                            kind="control",
                            summary="Exact source hash already has a successful payment.",
                        ),
                    ],
                }
            )
        return None

    def process_batch(self, directory: str | Path) -> BatchProcessResult:
        root = Path(directory).resolve()
        if not root.exists() or not root.is_dir():
            raise FileNotFoundError(f"Batch directory not found: {root}")
        paths = sorted(
            path
            for path in root.iterdir()
            if path.is_file() and path.suffix.casefold() in SUPPORTED_SUFFIXES
        )
        preflight: list[ProcessResult] = []
        errors: list[SourceProcessingError] = []
        for path in paths:
            try:
                result = self._exact_paid_result(path) or self.workflow.invoke(
                    path, defer_payment=True
                )
            except DocumentParseError as exc:
                errors.append(
                    SourceProcessingError(
                        source_file=path.name,
                        error_type=type(exc).__name__,
                        message=str(exc),
                    )
                )
                continue
            preflight.append(result)
        grouped: dict[tuple[str, str], list[ProcessResult]] = {}
        unidentified: list[ProcessResult] = []
        for result in preflight:
            if not result.invoice:
                unidentified.append(result)
                continue
            identity = (
                normalize_vendor(result.invoice.vendor_name),
                normalize_invoice_number(result.invoice.invoice_number),
            )
            if not all(identity):
                unidentified.append(result)
                continue
            grouped.setdefault(identity, []).append(result)

        logical_results = [self._settle_and_record(item) for item in unidentified]
        for group in grouped.values():
            logical_results.append(self._resolve_group(group))
        return BatchProcessResult(
            results=sorted(logical_results, key=lambda item: item.source_files[0]),
            errors=sorted(errors, key=lambda item: item.source_file),
        )

    def _resolve_group(self, group: list[ProcessResult]) -> ProcessResult:
        if len(group) == 1:
            return self._settle_and_record(group[0])
        fingerprints = {
            semantic_fingerprint(item.invoice) for item in group if item.invoice is not None
        }
        source_files, source_hashes = _unique_sources(group)
        base = group[0]
        if len(fingerprints) == 1 and len({item.final_status for item in group}) == 1:
            merged = base.model_copy(
                update={
                    "run_id": str(uuid.uuid4()),
                    "source_files": source_files,
                    "source_hashes": source_hashes,
                    "trace": [
                        *base.trace,
                        TraceEvent(
                            component="batch_deduplication",
                            kind="control",
                            summary=(
                                f"Collapsed {len(group)} semantically equivalent representations "
                                "before settlement."
                            ),
                        ),
                    ],
                }
            )
            return self._settle_and_record(merged)

        next_id = f"EVID-{len(base.validation.evidence) + 1:03d}"
        conflict = EvidenceRecord(
            id=next_id,
            code="VAL_REVISION_CONFLICT",
            status=EvidenceStatus.BLOCKING,
            message="Same invoice identity has conflicting content or workflow outcomes.",
            field="invoice_number",
            expected="semantically equivalent representations",
            actual=", ".join(source_files),
            required_outcome=FinalStatus.NEEDS_REVIEW,
        )
        validation = base.validation.model_copy(
            update={
                "evidence": [*base.validation.evidence, conflict],
                "route": "review",
            }
        )
        merged = base.model_copy(
            update={
                "run_id": str(uuid.uuid4()),
                "source_files": source_files,
                "source_hashes": source_hashes,
                "validation": validation,
                "final_status": FinalStatus.NEEDS_REVIEW,
                "payment": PaymentResult(
                    status=PaymentStatus.NOT_PAID,
                    message="Conflicting copies require review before settlement.",
                ),
                "trace": [
                    *base.trace,
                    TraceEvent(
                        component="batch_collision_guard",
                        kind="control",
                        status="warning",
                        summary="Conflicting representations were held before settlement.",
                    ),
                ],
            }
        )
        return self._settle_and_record(merged)

    def _settle_and_record(self, result: ProcessResult) -> ProcessResult:
        updated = result
        if (
            result.final_status == FinalStatus.APPROVED
            and result.invoice
            and result.payment.status != PaymentStatus.ALREADY_PAID
        ):
            invoice = result.invoice
            if invoice.vendor_name and invoice.invoice_number and invoice.total is not None:
                payment = self.repository.record_payment(
                    vendor=invoice.vendor_name,
                    invoice_number=invoice.invoice_number,
                    amount=invoice.total,
                    currency=invoice.currency or "USD",
                    source_hashes=result.source_hashes,
                )
                final = (
                    FinalStatus.NEEDS_REVIEW
                    if payment.status == PaymentStatus.ALREADY_PAID
                    else FinalStatus.APPROVED
                )
                updated = result.model_copy(
                    update={
                        "payment": payment,
                        "final_status": final,
                        "trace": [
                            *result.trace,
                            TraceEvent(
                                component="mock_payment",
                                kind="action",
                                summary=(
                                    f"Batch settlement status={payment.status.value}; "
                                    f"reference={payment.reference}."
                                ),
                            ),
                        ],
                    }
                )
        audited = updated.model_copy(
            update={
                "trace": [
                    *updated.trace,
                    TraceEvent(
                        timestamp=datetime.now(UTC),
                        component="audit",
                        kind="control",
                        summary="Persisted final batch result.",
                    ),
                ]
            }
        )
        self.repository.record_run(audited)
        return audited


def _unique_sources(results: list[ProcessResult]) -> tuple[list[str], list[str]]:
    source_files: list[str] = []
    source_hashes: list[str] = []
    seen_files: set[str] = set()
    seen_hashes: set[str] = set()
    for result in results:
        for source_file, source_hash in zip(
            result.source_files, result.source_hashes, strict=False
        ):
            if source_file in seen_files or source_hash in seen_hashes:
                continue
            seen_files.add(source_file)
            seen_hashes.add(source_hash)
            source_files.append(source_file)
            source_hashes.append(source_hash)
    return source_files, source_hashes
