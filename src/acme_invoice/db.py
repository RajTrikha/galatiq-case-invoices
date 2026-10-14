from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .models import PaymentResult, PaymentStatus, ProcessResult
from .utils import normalize_invoice_number, normalize_vendor, semantic_fingerprint

DEFAULT_INVENTORY = {
    "WidgetA": 15,
    "WidgetB": 10,
    "GadgetX": 5,
    "FakeItem": 0,
}


class InvoiceRepository:
    def __init__(self, inventory_path: Path, ledger_path: Path) -> None:
        self.inventory_path = inventory_path
        self.ledger_path = ledger_path
        self.inventory_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_inventory()
        self._initialize_ledger()

    def _connect_inventory(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.inventory_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _connect_ledger(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.ledger_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_inventory(self) -> None:
        with self._connect_inventory() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS inventory "
                "(item TEXT PRIMARY KEY, stock INTEGER NOT NULL)"
            )
            connection.executemany(
                "INSERT INTO inventory(item, stock) VALUES (?, ?) "
                "ON CONFLICT(item) DO UPDATE SET stock = excluded.stock",
                list(DEFAULT_INVENTORY.items()),
            )

    def _initialize_ledger(self) -> None:
        with self._connect_ledger() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    source_hash TEXT NOT NULL,
                    source_file TEXT NOT NULL,
                    vendor_key TEXT,
                    invoice_key TEXT,
                    final_status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS runs_identity_idx
                    ON runs(vendor_key, invoice_key, created_at);
                CREATE TABLE IF NOT EXISTS payments (
                    idempotency_key TEXT PRIMARY KEY,
                    reference TEXT NOT NULL UNIQUE,
                    vendor_key TEXT NOT NULL,
                    invoice_key TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    paid_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS payments_source_idx ON payments(source_hash);
                """
            )

    def inventory_snapshot(self) -> dict[str, int]:
        with self._connect_inventory() as connection:
            rows = connection.execute("SELECT item, stock FROM inventory ORDER BY item").fetchall()
        return {str(row["item"]): int(row["stock"]) for row in rows}

    def lookup_inventory(self, sku: str) -> dict[str, Any]:
        with self._connect_inventory() as connection:
            row = connection.execute(
                "SELECT item, stock FROM inventory WHERE lower(item) = lower(?)", (sku,)
            ).fetchone()
        if row is None:
            return {"found": False, "sku": sku, "stock": None}
        return {"found": True, "sku": str(row["item"]), "stock": int(row["stock"])}

    def lookup_prior_invoice(self, vendor: str, invoice_number: str) -> dict[str, Any]:
        vendor_key = normalize_vendor(vendor)
        invoice_key = normalize_invoice_number(invoice_number)
        with self._connect_ledger() as connection:
            runs = connection.execute(
                "SELECT run_id, final_status, created_at, source_file FROM runs "
                "WHERE vendor_key = ? AND invoice_key = ? ORDER BY created_at DESC LIMIT 10",
                (vendor_key, invoice_key),
            ).fetchall()
            payment = connection.execute(
                "SELECT reference, amount, currency, paid_at, source_hash FROM payments "
                "WHERE vendor_key = ? AND invoice_key = ?",
                (vendor_key, invoice_key),
            ).fetchone()
        return {
            "vendor_key": vendor_key,
            "invoice_key": invoice_key,
            "runs": [dict(row) for row in runs],
            "payment": dict(payment) if payment else None,
        }

    def payment_for_identity(
        self, vendor: str | None, invoice_number: str | None
    ) -> dict[str, Any] | None:
        vendor_key = normalize_vendor(vendor)
        invoice_key = normalize_invoice_number(invoice_number)
        if not vendor_key or not invoice_key:
            return None
        with self._connect_ledger() as connection:
            row = connection.execute(
                "SELECT * FROM payments WHERE vendor_key = ? AND invoice_key = ?",
                (vendor_key, invoice_key),
            ).fetchone()
        return dict(row) if row else None

    def payment_for_source(self, source_hash: str) -> dict[str, Any] | None:
        with self._connect_ledger() as connection:
            row = connection.execute(
                "SELECT * FROM payments WHERE source_hash = ? ORDER BY paid_at DESC LIMIT 1",
                (source_hash,),
            ).fetchone()
        return dict(row) if row else None

    def prior_invoice_fingerprints(
        self, vendor: str | None, invoice_number: str | None
    ) -> set[str]:
        vendor_key = normalize_vendor(vendor)
        invoice_key = normalize_invoice_number(invoice_number)
        if not vendor_key or not invoice_key:
            return set()
        with self._connect_ledger() as connection:
            rows = connection.execute(
                "SELECT result_json FROM runs WHERE vendor_key = ? AND invoice_key = ?",
                (vendor_key, invoice_key),
            ).fetchall()
        fingerprints: set[str] = set()
        for row in rows:
            prior = ProcessResult.model_validate_json(str(row["result_json"]))
            if prior.invoice:
                fingerprints.add(semantic_fingerprint(prior.invoice))
        return fingerprints

    def record_payment(
        self,
        *,
        vendor: str,
        invoice_number: str,
        amount: Decimal,
        currency: str,
        source_hash: str,
    ) -> PaymentResult:
        vendor_key = normalize_vendor(vendor)
        invoice_key = normalize_invoice_number(invoice_number)
        idempotency_key = f"{vendor_key}:{invoice_key}"
        reference_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()[:12].upper()
        reference = f"PAY-{reference_hash}"
        paid_at = datetime.now(UTC).isoformat()
        connection = self._connect_ledger()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT reference, amount, currency FROM payments WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                connection.rollback()
                return PaymentResult(
                    status=PaymentStatus.ALREADY_PAID,
                    reference=str(existing["reference"]),
                    amount=Decimal(str(existing["amount"])),
                    currency=str(existing["currency"]),
                    invoice_id=invoice_number,
                    message="Payment already exists; no second payment was created.",
                )
            connection.execute(
                "INSERT INTO payments(idempotency_key, reference, vendor_key, invoice_key, amount, "
                "currency, source_hash, paid_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    idempotency_key,
                    reference,
                    vendor_key,
                    invoice_key,
                    format(amount, "f"),
                    currency,
                    source_hash,
                    paid_at,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return PaymentResult(
            status=PaymentStatus.PAID,
            reference=reference,
            amount=amount,
            currency=currency,
            invoice_id=invoice_number,
            message="Mock payment recorded transactionally.",
        )

    def record_run(self, result: ProcessResult) -> None:
        invoice = result.invoice
        vendor_key = normalize_vendor(invoice.vendor_name) if invoice else ""
        invoice_key = normalize_invoice_number(invoice.invoice_number) if invoice else ""
        payload = result.model_dump_json()
        with self._connect_ledger() as connection:
            connection.execute(
                "INSERT INTO runs(run_id, source_hash, source_file, vendor_key, invoice_key, "
                "final_status, result_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    result.run_id,
                    result.source_hashes[0],
                    result.source_files[0],
                    vendor_key,
                    invoice_key,
                    result.final_status.value,
                    payload,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def load_paid_result_for_source(self, source_hash: str) -> ProcessResult | None:
        with self._connect_ledger() as connection:
            row = connection.execute(
                "SELECT result_json FROM runs WHERE source_hash = ? AND final_status = 'APPROVED' "
                "ORDER BY created_at DESC LIMIT 1",
                (source_hash,),
            ).fetchone()
        if not row:
            return None
        return ProcessResult.model_validate(json.loads(str(row["result_json"])))
