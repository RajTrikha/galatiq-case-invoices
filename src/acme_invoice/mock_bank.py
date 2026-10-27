"""The mock payment API supplied with the assignment, kept as a separate boundary.

This module stands in for the external bank. It is deliberately the only place that pretends to
move money, so the seam between our own ledger entry and the outbound call stays visible.

The one change from the supplied stub is the output stream: the CLI writes results as JSON on
stdout, so printing a settlement line there would corrupt that contract. It goes to stderr
alongside the other operational messages.
"""

from __future__ import annotations

import sys
from decimal import Decimal


def mock_payment(vendor: str, amount: Decimal | float) -> dict[str, str]:
    """Pretend to pay a vendor and report the outcome, as the assignment specifies."""
    print(f"Paid {amount} to {vendor}", file=sys.stderr)
    return {"status": "success"}
