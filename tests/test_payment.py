from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from acme_invoice.db import InvoiceRepository
from acme_invoice.models import PaymentStatus
from acme_invoice.service import InvoiceService
from acme_invoice.utils import normalize_invoice_number, normalize_vendor


def test_payment_insert_is_transactionally_idempotent(clean_invoice, repository) -> None:
    first = repository.record_payment(
        vendor=clean_invoice.vendor_name,
        invoice_number=clean_invoice.invoice_number,
        amount=clean_invoice.total,
        currency="USD",
        source_hashes=["a" * 64],
    )
    second = repository.record_payment(
        vendor=clean_invoice.vendor_name,
        invoice_number=clean_invoice.invoice_number,
        amount=clean_invoice.total,
        currency="USD",
        source_hashes=["b" * 64],
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

    first = service.process_batch(source_dir).results[0]
    second = service.process_batch(source_dir).results[0]

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


def test_explicit_revision_cannot_become_first_paid_representation(invoice_dir, tmp_path) -> None:
    service = InvoiceService(llm_mode="replay", runtime_dir=tmp_path)

    revised = service.process(invoice_dir / "invoice_1004_revised.json")
    original = service.process(invoice_dir / "invoice_1004.json")

    assert revised.final_status.value == "NEEDS_REVIEW"
    assert revised.payment.status == PaymentStatus.NOT_PAID
    assert original.final_status.value == "NEEDS_REVIEW"
    assert original.payment.status == PaymentStatus.NOT_PAID
    with sqlite3.connect(tmp_path / "ledger.db") as connection:
        assert connection.execute("SELECT count(*) FROM payments").fetchone()[0] == 0


def test_reference_is_deterministic_across_fresh_ledgers(clean_invoice, tmp_path) -> None:
    first_repo = InvoiceRepository(tmp_path / "one-i.db", tmp_path / "one-l.db")
    second_repo = InvoiceRepository(tmp_path / "two-i.db", tmp_path / "two-l.db")
    kwargs = {
        "vendor": clean_invoice.vendor_name,
        "invoice_number": clean_invoice.invoice_number,
        "amount": clean_invoice.total,
        "currency": "USD",
        "source_hashes": ["c" * 64],
    }
    first_reference = first_repo.record_payment(**kwargs).reference
    second_reference = second_repo.record_payment(**kwargs).reference
    assert first_reference == second_reference


def test_legacy_ledger_backfills_all_sources_from_stored_result(invoice_dir, tmp_path) -> None:
    source_runtime = tmp_path / "source-runtime"
    batch = InvoiceService(llm_mode="replay", runtime_dir=source_runtime).process_batch(
        invoice_dir
    )
    merged = next(
        result
        for result in batch.results
        if result.invoice and result.invoice.invoice_number == "INV-1011"
    )
    invoice = merged.invoice
    assert invoice is not None
    vendor_key = normalize_vendor(invoice.vendor_name)
    invoice_key = normalize_invoice_number(invoice.invoice_number)
    idempotency_key = f"{vendor_key}:{invoice_key}"
    legacy_ledger = tmp_path / "legacy-ledger.db"
    with sqlite3.connect(legacy_ledger) as connection:
        connection.executescript(
            """
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY, source_hash TEXT NOT NULL, source_file TEXT NOT NULL,
                vendor_key TEXT, invoice_key TEXT, final_status TEXT NOT NULL,
                result_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE payments (
                idempotency_key TEXT PRIMARY KEY, reference TEXT NOT NULL UNIQUE,
                vendor_key TEXT NOT NULL, invoice_key TEXT NOT NULL, amount TEXT NOT NULL,
                currency TEXT NOT NULL, source_hash TEXT NOT NULL, paid_at TEXT NOT NULL
            );
            """
        )
        now = datetime.now(UTC).isoformat()
        connection.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                merged.run_id,
                merged.source_hashes[0],
                merged.source_files[0],
                vendor_key,
                invoice_key,
                merged.final_status.value,
                merged.model_dump_json(),
                now,
            ),
        )
        connection.execute(
            "INSERT INTO payments VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                idempotency_key,
                merged.payment.reference,
                vendor_key,
                invoice_key,
                str(invoice.total),
                invoice.currency,
                merged.source_hashes[0],
                now,
            ),
        )

    repository = InvoiceRepository(tmp_path / "legacy-inventory.db", legacy_ledger)

    assert repository.payment_for_source(merged.source_hashes[1]) is not None
    restored = repository.load_paid_result_for_source(merged.source_hashes[1])
    assert restored is not None
    assert restored.source_hashes == merged.source_hashes
