from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any, Literal, cast

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from .agents import AgentSuite, verify_approval_evidence
from .db import InvoiceRepository
from .ingestion import canonicalize_invoice, load_document
from .llm import LLMClient, ReplayUnavailable
from .models import (
    ApprovalDecision,
    Critique,
    CritiqueVerdict,
    ExtractionResult,
    FinalStatus,
    Invoice,
    PaymentResult,
    PaymentStatus,
    ProcessResult,
    Recommendation,
    TraceEvent,
)
from .state import InvoiceState
from .validation import validate_invoice


class InvoiceWorkflow:
    def __init__(self, repository: InvoiceRepository, llm: LLMClient, llm_mode: str) -> None:
        self.repository = repository
        self.agents = AgentSuite(llm, repository)
        self.llm_mode = llm_mode
        self.graph = self._build_graph()

    def _build_graph(self):
        builder = StateGraph(InvoiceState)
        builder.add_node("load_parse", self._load_parse)
        builder.add_node("extract", self._extract)
        builder.add_node("canonical_guard", self._canonical_guard)
        builder.add_node("validate", self._validate)
        builder.add_node("approve", self._approve)
        builder.add_node("critic", self._critic)
        builder.add_node("decision_guard", self._decision_guard)
        builder.add_node("payment", self._payment)
        builder.add_node("audit", self._audit)

        builder.add_edge(START, "load_parse")
        builder.add_conditional_edges(
            "load_parse",
            lambda state: "extract" if state["requires_extraction"] else "canonical_guard",
            {"extract": "extract", "canonical_guard": "canonical_guard"},
        )
        builder.add_edge("extract", "canonical_guard")
        builder.add_conditional_edges(
            "canonical_guard",
            self._route_after_canonical_guard,
            {"extract": "extract", "validate": "validate"},
        )
        builder.add_conditional_edges(
            "validate",
            lambda state: "approve" if state["validation"].route == "viable" else "decision_guard",
            {"approve": "approve", "decision_guard": "decision_guard"},
        )
        builder.add_conditional_edges(
            "approve",
            lambda state: "critic" if state.get("critique_required", False) else "decision_guard",
            {"critic": "critic", "decision_guard": "decision_guard"},
        )
        builder.add_conditional_edges(
            "critic",
            self._route_after_critic,
            {"approve": "approve", "decision_guard": "decision_guard"},
        )
        builder.add_conditional_edges(
            "decision_guard",
            lambda state: (
                "payment"
                if state["final_status"] == FinalStatus.APPROVED
                and not state.get("defer_payment", False)
                else "audit"
            ),
            {"payment": "payment", "audit": "audit"},
        )
        builder.add_edge("payment", "audit")
        builder.add_edge("audit", END)
        return builder.compile()

    def invoke(self, source_path: Path, *, defer_payment: bool = False) -> ProcessResult:
        initial: InvoiceState = {
            "run_id": str(uuid.uuid4()),
            "source_path": str(source_path),
            "source_files": [source_path.name],
            "source_hashes": [],
            "llm_mode": self.llm_mode,
            "replayed": False,
            "trace": [],
            "extraction_attempts": 0,
            "approval_revision_count": 0,
            "defer_payment": defer_payment,
            "defer_audit": defer_payment,
        }
        state = self.graph.invoke(initial)
        return cast(ProcessResult, state["result"])

    def _load_parse(self, state: InvoiceState) -> dict[str, Any]:
        started = time.perf_counter()
        loaded = load_document(Path(state["source_path"]))
        extraction = ExtractionResult(
            invoice=loaded.invoice,
            confidence=1.0,
            attempts=0,
            origin="deterministic",
        )
        parse_method = "agent extraction" if loaded.requires_extraction else "deterministic parsing"
        return {
            "source_hash": loaded.source_hash,
            "source_hashes": [loaded.source_hash],
            "source_format": loaded.source_format,
            "raw_text": loaded.raw_text,
            "requires_extraction": loaded.requires_extraction,
            "invoice": loaded.invoice,
            "extraction": extraction,
            "trace": [
                _trace(
                    "load_parse",
                    "control",
                    f"Loaded {loaded.source_format.upper()} and selected {parse_method}.",
                    started,
                )
            ],
        }

    def _extract(self, state: InvoiceState) -> dict[str, Any]:
        started = time.perf_counter()
        attempt = state.get("extraction_attempts", 0) + 1
        try:
            response = self.agents.extract(
                source_hash=state["source_hash"],
                source_format=state["source_format"],
                raw_text=state["raw_text"],
                attempt=attempt,
                repair_feedback=state.get("repair_feedback"),
            )
            summary = (
                f"Extraction Agent returned a typed invoice at confidence "
                f"{response.data.confidence:.2f} ({response.origin})."
            )
            return {
                "invoice": response.data.invoice,
                "extraction": response.data,
                "extraction_attempts": attempt,
                "replayed": state.get("replayed", False) or response.replayed,
                "replay_origin": (
                    response.origin if response.replayed else state.get("replay_origin")
                ),
                "trace": [_trace("extraction_agent", "agent", summary, started)],
            }
        except ValidationError as exc:
            repair_feedback, unresolved_field = _targeted_repair_feedback(exc)
            extraction = ExtractionResult(
                invoice=None,
                confidence=0.0,
                unresolved_fields=[unresolved_field],
                warnings=[str(exc)],
                attempts=attempt,
                origin="invalid_structured_response",
            )
            return {
                "invoice": Invoice(source_format=state["source_format"]),
                "extraction": extraction,
                "extraction_attempts": attempt,
                "repair_feedback": repair_feedback,
                "trace": [
                    _trace(
                        "extraction_agent",
                        "agent",
                        f"Structured extraction failed validation at {unresolved_field}.",
                        started,
                        status="warning",
                    )
                ],
            }
        except Exception as exc:
            if self.llm_mode == "live" and not isinstance(exc, ReplayUnavailable):
                raise
            extraction = ExtractionResult(
                invoice=None,
                confidence=0.0,
                unresolved_fields=["invoice"],
                warnings=[str(exc)],
                attempts=attempt,
                origin="unavailable",
            )
            return {
                "invoice": Invoice(source_format=state["source_format"]),
                "extraction": extraction,
                "extraction_attempts": attempt,
                "repair_feedback": str(exc),
                "trace": [
                    _trace(
                        "extraction_agent",
                        "agent",
                        f"Extraction unavailable: {exc}",
                        started,
                        status="warning",
                    )
                ],
            }

    def _canonical_guard(self, state: InvoiceState) -> dict[str, Any]:
        started = time.perf_counter()
        extraction = state["extraction"]
        invoice = (
            state.get("invoice")
            or extraction.invoice
            or Invoice(source_format=state["source_format"])
        )
        errors: list[str] = []
        if extraction.unresolved_fields:
            errors.extend(f"unresolved field: {field}" for field in extraction.unresolved_fields)
        if extraction.invoice is None and state["requires_extraction"]:
            errors.append("Extraction did not produce a canonical invoice")
        normalized, events = canonicalize_invoice(
            invoice, list(self.repository.inventory_snapshot().keys())
        )
        normalized = normalized.model_copy(update={"source_format": state["source_format"]})
        if errors:
            feedback = state.get("repair_feedback") or (
                f"Field: {errors[0]}\nObserved: unresolved\nExpected: a value supported by "
                "the source\nInstruction: re-extract only this field from the original source; "
                "do not infer information that is not present."
            )
            status: Literal["completed", "warning"] = "warning"
            summary = f"Canonical guard found: {'; '.join(errors)}."
            updated_extraction = extraction.model_copy(
                update={"unresolved_fields": sorted(set(extraction.unresolved_fields + errors))}
            )
        else:
            feedback = ""
            status = "completed"
            summary = "Canonical schema and conservative normalizations passed."
            updated_extraction = extraction.model_copy(update={"invoice": normalized})
        return {
            "invoice": normalized,
            "extraction": updated_extraction,
            "normalizations": events,
            "canonical_errors": errors,
            "repair_feedback": feedback,
            "trace": [_trace("canonical_guard", "control", summary, started, status=status)],
        }

    @staticmethod
    def _route_after_canonical_guard(state: InvoiceState) -> str:
        if (
            state.get("canonical_errors")
            and state.get("requires_extraction")
            and state.get("extraction_attempts", 0) < 2
        ):
            return "extract"
        return "validate"

    def _validate(self, state: InvoiceState) -> dict[str, Any]:
        started = time.perf_counter()
        validation = validate_invoice(
            state["invoice"], state["extraction"], state["raw_text"], self.repository
        )
        return {
            "validation": validation,
            "trace": [
                _trace(
                    "deterministic_validation",
                    "control",
                    f"Validation produced {len(validation.evidence)} evidence records; "
                    f"route={validation.route}.",
                    started,
                )
            ],
        }

    def _approve(self, state: InvoiceState) -> dict[str, Any]:
        started = time.perf_counter()
        feedback = state.get("critique").feedback if state.get("critique") else None
        try:
            response = self.agents.approve(
                source_hash=state["source_hash"],
                invoice=state["invoice"],
                validation=state["validation"],
                critique_feedback=feedback,
            )
            approval = response.data
            evidence_valid, problems = verify_approval_evidence(approval, state["validation"])
            critique_required = True
            traces = [
                _trace(
                    "approval_agent",
                    "agent",
                    f"Approval Agent recommended {approval.recommendation.value}; "
                    f"evidence_valid={evidence_valid}.",
                    started,
                )
            ]
            traces.extend(
                TraceEvent(
                    component=call.name,
                    kind="tool",
                    summary=f"Read-only tool returned {call.result}.",
                )
                for call in approval.tool_calls
            )
            if problems:
                traces.append(
                    TraceEvent(
                        component="evidence_guard",
                        kind="control",
                        status="warning",
                        summary="; ".join(problems),
                    )
                )
            return {
                "approval": approval,
                "evidence_valid": evidence_valid,
                "critique_required": critique_required,
                "replayed": state.get("replayed", False) or response.replayed,
                "replay_origin": (
                    response.origin if response.replayed else state.get("replay_origin")
                ),
                "trace": traces,
            }
        except Exception as exc:
            if self.llm_mode == "live" and not isinstance(exc, ReplayUnavailable):
                raise
            approval = ApprovalDecision(
                recommendation=Recommendation.HOLD,
                rationale=f"Approval reasoning unavailable: {exc}",
                evidence_ids=[],
                risk_factors=["llm_unavailable"],
            )
            return {
                "approval": approval,
                "evidence_valid": False,
                "critique_required": False,
                "trace": [
                    _trace(
                        "approval_agent",
                        "agent",
                        str(exc),
                        started,
                        status="warning",
                    )
                ],
            }

    def _critic(self, state: InvoiceState) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            response = self.agents.critique(
                source_hash=state["source_hash"],
                invoice=state["invoice"],
                validation=state["validation"],
                approval=state["approval"],
            )
            critique = response.data
            reported_conflicts = (
                critique.unsupported_evidence_ids
                or critique.missed_evidence_ids
                or critique.policy_conflicts
            )
            if critique.verdict == CritiqueVerdict.ACCEPT and reported_conflicts:
                critique = critique.model_copy(
                    update={
                        "verdict": CritiqueVerdict.REVISE,
                        "feedback": critique.feedback
                        or "Resolve the conflicts reported by the Critic before payment.",
                    }
                )
            revision_count = state.get("approval_revision_count", 0)
            if critique.verdict == CritiqueVerdict.REVISE:
                revision_count += 1
            return {
                "critique": critique,
                "approval_revision_count": revision_count,
                "replayed": state.get("replayed", False) or response.replayed,
                "replay_origin": (
                    response.origin if response.replayed else state.get("replay_origin")
                ),
                "trace": [
                    _trace(
                        "critic_agent",
                        "agent",
                        f"Critic verdict: {critique.verdict.value}.",
                        started,
                    )
                ],
            }
        except Exception as exc:
            if self.llm_mode == "live" and not isinstance(exc, ReplayUnavailable):
                raise
            critique = Critique(
                verdict=CritiqueVerdict.REVISE,
                policy_conflicts=["critic_unavailable"],
                feedback=f"Critic reasoning unavailable: {exc}",
            )
            return {
                "critique": critique,
                "approval_revision_count": 2,
                "trace": [_trace("critic_agent", "agent", str(exc), started, status="warning")],
            }

    @staticmethod
    def _route_after_critic(state: InvoiceState) -> str:
        if (
            state["critique"].verdict == CritiqueVerdict.REVISE
            and state.get("approval_revision_count", 0) == 1
        ):
            return "approve"
        return "decision_guard"

    def _decision_guard(self, state: InvoiceState) -> dict[str, Any]:
        started = time.perf_counter()
        validation = state["validation"]
        payment = PaymentResult(status=PaymentStatus.NOT_PAID)
        if validation.route == "reject":
            final = FinalStatus.REJECTED
            reason = "Deterministic validation proved the invoice invalid."
        elif validation.route == "review":
            final = FinalStatus.NEEDS_REVIEW
            reason = "Deterministic validation found unresolved risk or uncertainty."
            prior = self.repository.payment_for_identity(
                state["invoice"].vendor_name, state["invoice"].invoice_number
            )
            if prior:
                payment = PaymentResult(
                    status=PaymentStatus.ALREADY_PAID,
                    reference=str(prior["reference"]),
                    amount=prior["amount"],
                    currency=str(prior["currency"]),
                    invoice_id=state["invoice"].invoice_number,
                    message="A prior payment exists; no payment was attempted.",
                )
        elif not state.get("approval"):
            final = FinalStatus.NEEDS_REVIEW
            reason = "No approval decision is available."
        elif not state.get("evidence_valid", False):
            final = FinalStatus.NEEDS_REVIEW
            reason = "Approval did not cite the required real evidence."
        elif state.get("critique_required") and (
            not state.get("critique") or state["critique"].verdict != CritiqueVerdict.ACCEPT
        ):
            final = FinalStatus.NEEDS_REVIEW
            reason = "Required independent critique did not accept the approval."
        elif state["approval"].recommendation == Recommendation.APPROVE:
            final = FinalStatus.APPROVED
            reason = "Validated evidence and approval reasoning passed the payment gate."
        elif state["approval"].recommendation == Recommendation.REJECT:
            final = FinalStatus.REJECTED
            reason = "Approval reasoning recommended rejection."
        else:
            final = FinalStatus.NEEDS_REVIEW
            reason = "Approval reasoning requested human review."
        return {
            "final_status": final,
            "payment": payment,
            "trace": [_trace("decision_guard", "control", f"{final.value}: {reason}", started)],
        }

    def _payment(self, state: InvoiceState) -> dict[str, Any]:
        started = time.perf_counter()
        invoice = state["invoice"]
        if not invoice.vendor_name or not invoice.invoice_number or invoice.total is None:
            raise RuntimeError("Payment gate received an incomplete invoice")
        payment = self.repository.record_payment(
            vendor=invoice.vendor_name,
            invoice_number=invoice.invoice_number,
            amount=invoice.total,
            currency=invoice.currency or "USD",
            source_hash=state["source_hash"],
        )
        final = (
            FinalStatus.NEEDS_REVIEW
            if payment.status == PaymentStatus.ALREADY_PAID
            else FinalStatus.APPROVED
        )
        return {
            "payment": payment,
            "final_status": final,
            "trace": [
                _trace(
                    "mock_payment",
                    "action",
                    f"Payment status={payment.status.value}; reference={payment.reference}.",
                    started,
                )
            ],
        }

    def _audit(self, state: InvoiceState) -> dict[str, Any]:
        started = time.perf_counter()
        audit_event = _trace(
            "audit",
            "control",
            "Persisted an append-only run result and trace.",
            started,
        )
        trace = [*state.get("trace", []), audit_event]
        result = ProcessResult(
            run_id=state["run_id"],
            source_files=state["source_files"],
            source_hashes=state["source_hashes"],
            llm_mode=cast(Any, state["llm_mode"]),
            replayed=state.get("replayed", False),
            replay_origin=state.get("replay_origin"),
            invoice=state.get("invoice"),
            normalizations=state.get("normalizations", []),
            validation=state["validation"],
            approval=state.get("approval"),
            critique=state.get("critique"),
            final_status=state["final_status"],
            payment=state.get("payment", PaymentResult()),
            trace=trace,
        )
        if not state.get("defer_audit", False):
            self.repository.record_run(result)
        return {"result": result, "trace": [audit_event]}


def _trace(
    component: str,
    kind: Literal["control", "agent", "tool", "action"],
    summary: str,
    started: float,
    *,
    status: Literal["completed", "warning", "failed"] = "completed",
) -> TraceEvent:
    return TraceEvent(
        component=component,
        kind=kind,
        status=status,
        summary=summary,
        duration_ms=max(0, round((time.perf_counter() - started) * 1000)),
    )


def _targeted_repair_feedback(exc: ValidationError) -> tuple[str, str]:
    first = exc.errors(include_url=False)[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or "invoice"
    observed = first.get("input", "unresolved")
    expectation = first.get("msg", "a value matching the canonical invoice schema")
    return (
        f"Field: {location}\nObserved: {observed!r}\nExpected: {expectation}\n"
        "Instruction: re-extract only this field from the original source; do not infer "
        "information that is not present.",
        location,
    )
