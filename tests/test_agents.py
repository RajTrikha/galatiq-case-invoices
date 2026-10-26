from __future__ import annotations

from acme_invoice.agents import AgentSuite, verify_approval_evidence
from acme_invoice.llm import FakeLLMClient
from acme_invoice.models import (
    ApprovalDecision,
    EvidenceRecord,
    EvidenceStatus,
    Recommendation,
    ValidationResult,
)


def test_approval_agent_invokes_real_read_only_tools(clean_invoice, repository) -> None:
    validation = ValidationResult(
        evidence=[
            EvidenceRecord(
                id="EVID-001",
                code="VAL_STOCK_SUFFICIENT",
                status=EvidenceStatus.PASSED,
                message="stock ok",
            )
        ]
    )
    client = FakeLLMClient(
        {
            "approval": {
                "tool_requests": [
                    {"name": "lookup_inventory", "arguments": {"sku": "WidgetA"}},
                    {
                        "name": "lookup_prior_invoice",
                        "arguments": {
                            "vendor": clean_invoice.vendor_name,
                            "invoice_number": clean_invoice.invoice_number,
                        },
                    },
                ],
                "response": {
                    "recommendation": "approve",
                    "rationale": "Validated",
                    "evidence_ids": ["EVID-001"],
                    "risk_factors": [],
                },
            }
        }
    )
    response = AgentSuite(client, repository).approve(
        source_hash="a" * 64,
        invoice=clean_invoice,
        validation=validation,
    )
    assert [call.name for call in response.data.tool_calls] == [
        "lookup_inventory",
        "lookup_prior_invoice",
    ]
    assert response.data.tool_calls[0].result == {
        "found": True,
        "sku": "WidgetA",
        "stock": 15,
    }


def test_code_aliases_resolve_to_real_evidence_ids(clean_invoice, repository) -> None:
    validation = ValidationResult(
        evidence=[
            EvidenceRecord(
                id="EVID-007",
                code="VAL_CURRENCY_SUPPORTED",
                status=EvidenceStatus.PASSED,
                message="USD",
            )
        ]
    )
    client = FakeLLMClient(
        {
            "approval": {
                "response": {
                    "recommendation": "hold",
                    "rationale": "testing",
                    "evidence_ids": ["CODE:VAL_CURRENCY_SUPPORTED"],
                    "risk_factors": [],
                }
            }
        }
    )
    response = AgentSuite(client, repository).approve(
        source_hash="a" * 64,
        invoice=clean_invoice,
        validation=validation,
    )
    assert response.data.evidence_ids == ["EVID-007"]


def test_clean_approval_must_cite_positive_financial_controls(clean_invoice) -> None:
    evidence = [
        EvidenceRecord(
            id=f"EVID-{index:03d}",
            code=code,
            status=EvidenceStatus.PASSED,
            message=code,
        )
        for index, code in enumerate(
            (
                "VAL_REQUIRED_FIELDS_PRESENT",
                "VAL_TOTAL_RECONCILED",
                "VAL_STOCK_SUFFICIENT",
                "VAL_CURRENCY_SUPPORTED",
                "VAL_NO_PRIOR_PAYMENT",
            ),
            start=1,
        )
    ]
    approval = ApprovalDecision(
        recommendation=Recommendation.APPROVE,
        rationale="Looks good",
        evidence_ids=["EVID-001"],
    )
    valid, problems = verify_approval_evidence(
        approval, ValidationResult(evidence=evidence), clean_invoice
    )
    assert not valid
    assert len(problems) >= 4


def test_fabricated_evidence_is_invalid(clean_invoice) -> None:
    validation = ValidationResult(
        evidence=[
            EvidenceRecord(
                id="EVID-001",
                code="VAL_TOTAL_RECONCILED",
                status=EvidenceStatus.PASSED,
                message="ok",
            )
        ]
    )
    approval = ApprovalDecision(
        recommendation=Recommendation.HOLD,
        rationale="Unsupported duplicate claim",
        evidence_ids=["EVID-999"],
    )
    valid, problems = verify_approval_evidence(approval, validation, clean_invoice)
    assert not valid
    assert "unsupported evidence EVID-999" in problems


def test_agent_cannot_override_blocking_evidence(clean_invoice) -> None:
    validation = ValidationResult(
        evidence=[
            EvidenceRecord(
                id="EVID-001",
                code="VAL_TOTAL_MISMATCH",
                status=EvidenceStatus.BLOCKING,
                message="bad total",
            )
        ]
    )
    approval = ApprovalDecision(
        recommendation=Recommendation.APPROVE,
        rationale="Pay anyway",
        evidence_ids=["EVID-001"],
    )
    valid, problems = verify_approval_evidence(approval, validation, clean_invoice)
    assert not valid
    assert "approval attempted to override blocking deterministic evidence" in problems


def test_tool_result_can_invalidate_an_approval(clean_invoice) -> None:
    from acme_invoice.models import ToolCallRecord

    validation = ValidationResult(
        evidence=[
            EvidenceRecord(
                id="EVID-001",
                code="VAL_STOCK_SUFFICIENT",
                status=EvidenceStatus.PASSED,
                message="stock was validated earlier",
            )
        ],
        aggregated_quantities={"WidgetA": 2},
    )
    approval = ApprovalDecision(
        recommendation=Recommendation.APPROVE,
        rationale="Approve despite the fresh tool result",
        evidence_ids=["EVID-001"],
        tool_calls=[
            ToolCallRecord(
                name="lookup_inventory",
                arguments={"sku": "WidgetA"},
                result={"found": True, "sku": "WidgetA", "stock": 1},
            )
        ],
    )
    valid, problems = verify_approval_evidence(approval, validation, clean_invoice)
    assert not valid
    assert "inventory tool reported insufficient stock for approved SKU 'WidgetA'" in problems
