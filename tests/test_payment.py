from __future__ import annotations

import sqlite3

from acme_invoice.models import PaymentStatus
from acme_invoice.service import InvoiceService


def test_payment_insert_is_transactionally_idempotent(clean_invoice, repository) -> None:
    first = repository.record_payment(
        vendor=clean_invoice.vendor_name,
        invoice_number=clean_invoice.invoice_number,
        amount=clean_invoice.total,
        currency="USD",
        source_hash="a" * 64,
    )
    second = repository.record_payment(
        vendor=clean_invoice.vendor_name,
        invoice_number=clean_invoice.invoice_number,
        amount=clean_invoice.total,
        currency="USD",
        source_hash="b" * 64,
    )
    assert first.status == PaymentStatus.PAID
    assert second.status == PaymentStatus.ALREADY_PAID
    assert first.reference == second.reference


def test_exact_paid_source_returns_prior_result_without_second_payment(
    invoice_dir, tmp_path
) -> None:
    service = InvoiceService(llm_mode="replay", runtime_dir=tmp_path)
    first = service.process(invoice_dir / "invoice_1004.json")
    second = service.process(invoice_dir / "invoice_1004.json")
    assert first.payment.status == PaymentStatus.PAID
    assert second.payment.status == PaymentStatus.ALREADY_PAID
    with sqlite3.connect(tmp_path / "ledger.db") as connection:
        assert connection.execute("SELECT count(*) FROM payments").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 2


def test_exact_paid_source_batch_rerun_returns_prior_success(invoice_dir, tmp_path) -> None:
    source_dir = tmp_path / "invoices"
    source_dir.mkdir()
    source = source_dir / "invoice_1004.json"
    source.write_bytes((invoice_dir / "invoice_1004.json").read_bytes())
    service = InvoiceService(llm_mode="replay", runtime_dir=tmp_path / "runtime")

    first = service.process_batch(source_dir)[0]
    second = service.process_batch(source_dir)[0]

    assert first.payment.status == PaymentStatus.PAID
    assert second.final_status.value == "APPROVED"
    assert second.payment.status == PaymentStatus.ALREADY_PAID
    with sqlite3.connect(tmp_path / "runtime" / "ledger.db") as connection:
        assert connection.execute("SELECT count(*) FROM payments").fetchone()[0] == 1


def test_reviewed_source_is_reprocessed_and_history_is_preserved(invoice_dir, tmp_path) -> None:
    service = InvoiceService(llm_mode="replay", runtime_dir=tmp_path)
    first = service.process(invoice_dir / "invoice_1016.json")
    second = service.process(invoice_dir / "invoice_1016.json")
    assert first.run_id != second.run_id
    with sqlite3.connect(tmp_path / "ledger.db") as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM payments").fetchone()[0] == 0


def test_different_source_with_paid_identity_is_held(invoice_dir, tmp_path) -> None:
    service = InvoiceService(llm_mode="replay", runtime_dir=tmp_path / "runtime")
    first = service.process(invoice_dir / "invoice_1004.json")
    altered = tmp_path / "same_identity.json"
    altered.write_text((invoice_dir / "invoice_1004.json").read_text() + "\n")
    second = service.process(altered)
    assert first.payment.status == PaymentStatus.PAID
    assert second.payment.status == PaymentStatus.ALREADY_PAID
    assert second.final_status.value == "NEEDS_REVIEW"
    assert any(item.code == "VAL_DUPLICATE_PAID" for item in second.validation.evidence)


def test_explicit_revision_after_payment_is_held(invoice_dir, tmp_path) -> None:
    service = InvoiceService(llm_mode="replay", runtime_dir=tmp_path)
    service.process(invoice_dir / "invoice_1004.json")
    revised = service.process(invoice_dir / "invoice_1004_revised.json")
    assert revised.final_status.value == "NEEDS_REVIEW"
    assert revised.payment.status == PaymentStatus.ALREADY_PAID
    assert any(item.code == "VAL_REVISION_CONFLICT" for item in revised.validation.evidence)


def test_reference_is_deterministic_across_fresh_ledgers(clean_invoice, tmp_path) -> None:
    from acme_invoice.db import InvoiceRepository

    first_repo = InvoiceRepository(tmp_path / "one-i.db", tmp_path / "one-l.db")
    second_repo = InvoiceRepository(tmp_path / "two-i.db", tmp_path / "two-l.db")
    kwargs = {
        "vendor": clean_invoice.vendor_name,
        "invoice_number": clean_invoice.invoice_number,
        "amount": clean_invoice.total,
        "currency": "USD",
        "source_hash": "c" * 64,
    }
    first_reference = first_repo.record_payment(**kwargs).reference
    second_reference = second_repo.record_payment(**kwargs).reference
    assert first_reference == second_reference
