from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .llm import LLMConfigurationError
from .service import InvoiceService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Process Acme invoices through a safe LangGraph workflow."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--invoice_path", type=Path, help="Path to one invoice document.")
    source.add_argument("--batch", type=Path, help="Directory of invoice documents.")
    parser.add_argument("--out", type=Path, help="Write JSON Lines results to this path.")
    parser.add_argument(
        "--llm-mode",
        choices=("auto", "live", "replay"),
        default="auto",
        help="Use live Grok, transparent fixture replay, or automatic selection.",
    )
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=Path(".runtime"),
        help="Directory for local inventory and payment ledger databases.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    errors = []
    try:
        service = InvoiceService(llm_mode=args.llm_mode, runtime_dir=args.runtime_dir)
        if args.invoice_path:
            results = [service.process(args.invoice_path)]
        else:
            batch = service.process_batch(args.batch)
            results = batch.results
            errors = batch.errors
    except (FileNotFoundError, ValueError, LLMConfigurationError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}), file=sys.stderr)
        return 1

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        records = [result.model_dump_json() for result in results]
        records.extend(error.model_dump_json() for error in errors)
        args.out.write_text("".join(record + "\n" for record in records))
        print(
            f"Wrote {len(results)} result(s) and {len(errors)} error(s) to {args.out}",
            file=sys.stderr,
        )
    elif args.invoice_path and len(results) == 1:
        print(results[0].model_dump_json(indent=2))
    else:
        for result in results:
            print(result.model_dump_json())
        for error in errors:
            print(error.model_dump_json())
    if errors:
        print(f"Batch completed with {len(errors)} document error(s).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
