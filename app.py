from __future__ import annotations

import hashlib
import os
from pathlib import Path

import streamlit as st

from acme_invoice.llm import GrokClient
from acme_invoice.models import EvidenceStatus, FinalStatus, ProcessResult
from acme_invoice.service import InvoiceService

PROJECT_ROOT = Path(__file__).resolve().parent
SAMPLE_DIR = PROJECT_ROOT / "data" / "invoices"
RUNTIME_DIR = PROJECT_ROOT / ".runtime" / "streamlit"
SUPPORTED_SUFFIXES = ["txt", "pdf", "json", "csv", "xml"]


def _status_banner(result: ProcessResult) -> None:
    message = f"{result.final_status.value} · {result.payment.status.value.replace('_', ' ')}"
    if result.final_status == FinalStatus.APPROVED:
        st.success(message)
    elif result.final_status == FinalStatus.REJECTED:
        st.error(message)
    else:
        st.warning(message)


def _evidence_rows(result: ProcessResult, status: EvidenceStatus) -> list[dict[str, str]]:
    return [
        {
            "ID": record.id,
            "Code": record.code,
            "Field": record.field or "—",
            "Message": record.message,
            "Expected": record.expected or "—",
            "Actual": record.actual or "—",
        }
        for record in result.validation.evidence
        if record.status == status
    ]


def render_result(result: ProcessResult) -> None:
    _status_banner(result)
    if result.replayed:
        origin = result.replay_origin or "recording"
        st.info(
            f"Replay mode: agent responses came from a matched {origin}, not a live model call."
        )

    invoice = result.invoice
    columns = st.columns(4)
    columns[0].metric("Vendor", invoice.vendor_name if invoice else "—")
    columns[1].metric("Invoice", invoice.invoice_number if invoice else "—")
    total = (
        f"{invoice.currency or ''} {invoice.total:,.2f}"
        if invoice and invoice.total is not None
        else "—"
    )
    columns[2].metric("Total", total)
    columns[3].metric("Mock payment", result.payment.status.value.replace("_", " ").title())

    evidence_tab, invoice_tab, reasoning_tab, trace_tab = st.tabs(
        ["Validation evidence", "Canonical invoice", "Agent reasoning", "Workflow trace"]
    )
    with evidence_tab:
        for status, heading in (
            (EvidenceStatus.BLOCKING, "Blocking"),
            (EvidenceStatus.WARNING, "Warnings"),
            (EvidenceStatus.PASSED, "Passed controls"),
        ):
            rows = _evidence_rows(result, status)
            st.subheader(f"{heading} ({len(rows)})")
            if rows:
                st.dataframe(rows, hide_index=True, width="stretch")
            else:
                st.caption("None")

    with invoice_tab:
        st.json(invoice.model_dump(mode="json") if invoice else {})
        st.subheader("Normalization events")
        if result.normalizations:
            st.dataframe(
                [event.model_dump(mode="json") for event in result.normalizations],
                hide_index=True,
                width="stretch",
            )
        else:
            st.caption("No normalization was needed.")

    with reasoning_tab:
        st.subheader("Approval Agent")
        if result.approval:
            st.write(result.approval.rationale)
            st.caption(
                f"Recommendation: {result.approval.recommendation.value} · "
                f"Evidence: {', '.join(result.approval.evidence_ids) or 'none'}"
            )
            st.subheader("Read-only tool calls")
            if result.approval.tool_calls:
                st.dataframe(
                    [call.model_dump(mode="json") for call in result.approval.tool_calls],
                    hide_index=True,
                    width="stretch",
                )
            else:
                st.caption("No tool calls were made.")
        else:
            st.caption("Deterministic routing completed before approval reasoning was needed.")
        st.subheader("Critic Agent")
        if result.critique:
            st.write(result.critique.feedback or "No revision feedback was needed.")
            st.caption(f"Verdict: {result.critique.verdict.value}")
        else:
            st.caption("No Approval decision was produced, so the Critic was not invoked.")

    with trace_tab:
        st.dataframe(
            [
                {
                    "Time": event.timestamp.isoformat(timespec="seconds"),
                    "Component": event.component,
                    "Type": event.kind,
                    "Status": event.status,
                    "Summary": event.summary,
                    "ms": event.duration_ms,
                }
                for event in result.trace
            ],
            hide_index=True,
            width="stretch",
        )
        if result.payment.reference:
            st.caption(f"Deterministic mock reference: {result.payment.reference}")


def _persist_upload(uploaded_file) -> Path:
    content = uploaded_file.getvalue()
    digest = hashlib.sha256(content).hexdigest()[:12]
    safe_suffix = Path(uploaded_file.name).suffix.casefold()
    target_dir = RUNTIME_DIR / "uploads"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{digest}{safe_suffix}"
    target.write_bytes(content)
    return target


def main() -> None:
    st.set_page_config(page_title="Acme Invoice Automation", page_icon="🧾", layout="wide")
    st.title("Acme Invoice Automation")
    st.caption("Agents interpret. Code verifies. Code pays.")
    st.write(
        "A bounded three-agent LangGraph workflow with deterministic validation, an "
        "evidence-checked payment gate, and an idempotent mock ledger."
    )

    input_column, mode_column = st.columns([3, 1])
    with input_column:
        source_kind = st.radio("Invoice source", ["Bundled sample", "Upload"], horizontal=True)
        source_path: Path | None = None
        if source_kind == "Bundled sample":
            samples = sorted(
                path
                for path in SAMPLE_DIR.iterdir()
                if path.suffix.casefold()[1:] in SUPPORTED_SUFFIXES
            )
            selected = st.selectbox("Sample invoice", samples, format_func=lambda path: path.name)
            source_path = selected
        else:
            upload = st.file_uploader("Upload an invoice", type=SUPPORTED_SUFFIXES)
            if upload is not None:
                source_path = _persist_upload(upload)
    with mode_column:
        llm_mode = st.selectbox("LLM mode", ["auto", "replay", "live"])
        st.caption("Replay is visibly marked and only matches bundled fixture hashes.")

    api_key = ""
    model = os.getenv("LLM_MODEL") or "grok-4.7"
    base_url = os.getenv("LLM_BASE_URL") or "https://api.x.ai/v1"
    if llm_mode in {"auto", "live"}:
        with st.container(border=True):
            st.subheader("Live model credentials")
            credential_column, model_column = st.columns([2, 1])
            with credential_column:
                api_key = st.text_input(
                    "xAI API key",
                    type="password",
                    help=(
                        "Held only in this Streamlit session; never written to files, SQLite, "
                        "or traces."
                    ),
                    placeholder="Paste an xAI API key",
                )
            with model_column:
                model = st.text_input("Model", value=model)
                base_url = st.text_input("Base URL", value=base_url)
            if llm_mode == "auto":
                st.caption(
                    "With a key, Auto uses the live model. Without one, it uses a matching "
                    "transparent replay for bundled fixtures."
                )
            else:
                st.caption("Live mode requires a valid key. The key is kept in memory only.")

    if st.button("Process invoice", type="primary", disabled=source_path is None):
        try:
            with st.spinner("Running guarded invoice workflow…"):
                live_client = (
                    GrokClient(api_key=api_key, model=model, base_url=base_url) if api_key else None
                )
                service = InvoiceService(
                    llm_mode=llm_mode,
                    runtime_dir=RUNTIME_DIR,
                    llm_client=live_client,
                )
                st.session_state["invoice_result"] = service.process(source_path)
        except Exception as exc:  # Streamlit should surface configuration/infrastructure failures.
            st.session_state.pop("invoice_result", None)
            st.error(f"Processing failed: {exc}")

    result = st.session_state.get("invoice_result")
    if result:
        st.divider()
        render_result(result)


if __name__ == "__main__":
    main()
