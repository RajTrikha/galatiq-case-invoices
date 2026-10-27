"""SQLite inventory reference, append-only run history, and the idempotent payment ledger.

The only module that writes financial state, and the only one that may record a payment.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .canonical import normalize_invoice_number, normalize_vendor, semantic_fingerprint
from .models import PaymentResult, PaymentStatus, ProcessResult

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
        with closing(self._connect_inventory()) as connection, connection:
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
        with closing(self._connect_ledger()) as connection, connection:
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
                CREATE TABLE IF NOT EXISTS run_sources (
                    run_id TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    source_file TEXT NOT NULL,
                    PRIMARY KEY (run_id, source_hash)
                );
                CREATE INDEX IF NOT EXISTS run_sources_hash_idx
                    ON run_sources(source_hash);
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
                CREATE TABLE IF NOT EXISTS payment_sources (
                    idempotency_key TEXT NOT NULL,
                    source_hash TEXT NOT NULL UNIQUE,
                    PRIMARY KEY (idempotency_key, source_hash)
                );
                """
            )
            self._backfill_source_mappings(connection)

    @staticmethod
    def _backfill_source_mappings(connection: sqlite3.Connection) -> None:
        payments = connection.execute(
            "SELECT idempotency_key, source_hash FROM payments"
        ).fetchall()
        connection.executemany(
            "INSERT OR IGNORE INTO payment_sources(idempotency_key, source_hash) VALUES (?, ?)",
            [(row["idempotency_key"], row["source_hash"]) for row in payments],
        )

        rows = connection.execute(
            "SELECT run_id, source_hash, source_file, vendor_key, invoice_key, final_status, "
            "result_json FROM runs"
        ).fetchall()
        for row in rows:
            try:
                result = ProcessResult.model_validate_json(str(row["result_json"]))
                pairs = _source_pairs(result)
            except ValueError:
                pairs = [(str(row["source_hash"]), str(row["source_file"]))]
            connection.executemany(
                "INSERT OR IGNORE INTO run_sources(run_id, source_hash, source_file) "
                "VALUES (?, ?, ?)",
                [(row["run_id"], source_hash, source_file) for source_hash, source_file in pairs],
            )
            if row["final_status"] != "APPROVED":
                continue
            payment = connection.execute(
                "SELECT idempotency_key FROM payments WHERE vendor_key = ? AND invoice_key = ?",
                (row["vendor_key"], row["invoice_key"]),
            ).fetchone()
            if payment:
                connection.executemany(
                    "INSERT OR IGNORE INTO payment_sources(idempotency_key, source_hash) "
                    "VALUES (?, ?)",
                    [(payment["idempotency_key"], source_hash) for source_hash, _ in pairs],
                )

    def inventory_snapshot(self) -> dict[str, int]:
        with closing(self._connect_inventory()) as connection, connection:
            rows = connection.execute("SELECT item, stock FROM inventory ORDER BY item").fetchall()
        return {str(row["item"]): int(row["stock"]) for row in rows}

    def lookup_inventory(self, sku: str) -> dict[str, Any]:
        with closing(self._connect_inventory()) as connection, connection:
            row = connection.execute(
                "SELECT item, stock FROM inventory WHERE lower(item) = lower(?)", (sku,)
            ).fetchone()
        if row is None:
            return {"found": False, "sku": sku, "stock": None}
        return {"found": True, "sku": str(row["item"]), "stock": int(row["stock"])}

    def lookup_prior_invoice(self, vendor: str, invoice_number: str) -> dict[str, Any]:
        vendor_key = normalize_vendor(vendor)
        invoice_key = normalize_invoice_number(invoice_number)
        with closing(self._connect_ledger()) as connection, connection:
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
        with closing(self._connect_ledger()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM payments WHERE vendor_key = ? AND invoice_key = ?",
                (vendor_key, invoice_key),
            ).fetchone()
        return dict(row) if row else None

    def payment_for_source(self, source_hash: str) -> dict[str, Any] | None:
        with closing(self._connect_ledger()) as connection, connection:
            row = connection.execute(
                "SELECT payments.* FROM payments "
                "JOIN payment_sources USING(idempotency_key) "
                "WHERE payment_sources.source_hash = ? ORDER BY paid_at DESC LIMIT 1",
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
        with closing(self._connect_ledger()) as connection, connection:
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
        source_hashes: list[str],
    ) -> PaymentResult:
        unique_hashes = list(dict.fromkeys(source_hashes))
        if not unique_hashes:
            raise ValueError("At least one source hash is required to record a payment")
        vendor_key = normalize_vendor(vendor)
        invoice_key = normalize_invoice_number(invoice_number)
        idempotency_key = f"{vendor_key}:{invoice_key}"
        reference_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()[:12].upper()
        reference = f"PAY-{reference_hash}"
        paid_at = datetime.now(UTC).isoformat()
        # Managed by hand rather than with contextlib.closing: the check-and-insert needs an
        # explicit BEGIN IMMEDIATE so a concurrent writer cannot slip a second payment between
        # the SELECT and the INSERT, and the duplicate branch rolls back rather than commits.
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
                    unique_hashes[0],
                    paid_at,
                ),
            )
            connection.executemany(
                "INSERT INTO payment_sources(idempotency_key, source_hash) VALUES (?, ?)",
                [(idempotency_key, source_hash) for source_hash in unique_hashes],
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
        with closing(self._connect_ledger()) as connection, connection:
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
            connection.executemany(
                "INSERT INTO run_sources(run_id, source_hash, source_file) VALUES (?, ?, ?)",
                [
                    (result.run_id, source_hash, source_file)
                    for source_hash, source_file in _source_pairs(result)
                ],
            )

    def load_paid_result_for_source(self, source_hash: str) -> ProcessResult | None:
        with closing(self._connect_ledger()) as connection, connection:
            row = connection.execute(
                "SELECT runs.result_json FROM runs "
                "JOIN run_sources USING(run_id) "
                "WHERE run_sources.source_hash = ? AND runs.final_status = 'APPROVED' "
                "ORDER BY created_at DESC LIMIT 1",
                (source_hash,),
            ).fetchone()
        if not row:
            return None
        return ProcessResult.model_validate(json.loads(str(row["result_json"])))


def _source_pairs(result: ProcessResult) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    seen_hashes: set[str] = set()
    seen_files: set[str] = set()
    for source_hash, source_file in zip(result.source_hashes, result.source_files, strict=False):
        if source_hash in seen_hashes or source_file in seen_files:
            continue
        seen_hashes.add(source_hash)
        seen_files.add(source_file)
        pairs.append((source_hash, source_file))
    if not pairs:
        raise ValueError("A process result must contain at least one source hash and filename")
    return pairs
