# Technical notes

Supporting detail for [the README](../README.md). Nothing here is required to review or run the
project; it records the reasoning behind decisions that are easy to mistake for oversights.

## Source-figure grounding

Arithmetic reconciliation proves an invoice is *internally* consistent, not that it matches the
document it came from. A self-consistent hallucination therefore passed every arithmetic,
inventory, and approval control: a TXT source reading `$500.00` could be extracted as
`$12,500.00`, satisfy every total check because the fabricated figures agreed with each other,
and be paid in full.

For TXT and PDF — the only formats an agent interprets rather than a parser reads — every
payment-critical figure must now appear in the source document.

Comparison is **numeric, not textual**. INV-1002 prints `$750` and INV-1012 prints `$250`, so a
string search for `750.00` would reject valid extractions. Every numeric literal in the source is
collected into a set of `Decimal` values after normalizing currency symbols, thousands
separators, and OCR-style `O`/`0` confusion; `Decimal("750.00") == Decimal("750")`, so precision
differences resolve themselves.

| Rule | Reason |
|---|---|
| `total` must be grounded | it sets the amount paid |
| non-zero `subtotal` / `tax` / `freight` must be grounded | they compose the total |
| every `quantity` and `unit_price` must be grounded | the inputs the total is built from |
| derived line amounts are exempt | INV-1001 and INV-1008 print quantity and unit price without the product |
| a zero `subtotal` / `tax` / `freight` is exempt | it asserts an absent charge, and most documents print no standalone `0` |

Mismatches emit blocking `VAL_SOURCE_FIGURE_UNSUPPORTED` and route to `NEEDS_REVIEW` before the
Approval Agent runs. A clean pass emits `VAL_SOURCE_FIGURES_GROUNDED`.

**What this does and does not prove.** Grounding protects *the amount paid*: the total must be
printed in the source and the arithmetic must close against it, so a fabricated total cannot
survive both checks. It is membership against the document's full set of numbers, so it does not
prove line-by-line fidelity — an extraction could still redistribute grounded values across line
items without changing the total. That is a reporting-accuracy limit, not a payment-safety one.

## Why validation is not an agent

The assignment refers to a validation agent, and this build answers that stage with deterministic
code instead. The reasoning is that validation reduces to arithmetic and set operations, which is
where a language model is least reliable and where the supplied corpus is most adversarial.

Three defects in the supplied data make the cost concrete:

- **INV-1013 hides $50.** Line items sum to 21,040.00 and 7% tax is 1,472.80, so the total should
  be 22,512.80. It prints 22,562.80. The JSON and the PDF report the same inflated figure, so
  cross-format agreement cannot catch it — only independent recomputation can.
- **INV-1007 is $110 short.** The subtotal is right and the stated 6% tax is right; the printed
  total still does not follow from them.
- **INV-1013 passes stock line by line and fails in aggregate.** All eight line items are within
  stock individually. Summed per canonical SKU, WidgetA needs 22 against 15, WidgetB 18 against
  10, and GadgetX 9 against 5. A validator that iterates line items without grouping first gives
  this invoice a clean bill of health.

A model could get any one of these right. Relying on it to get all three right on every invoice,
every run, is a worse bet than `Decimal` arithmetic. The same judgment produced source grounding:
figures that an extraction agent reports cannot be taken on trust, so they are checked against the
document before they can influence a payment.

What the agents do instead is the work that genuinely needs judgment — reading a messy or
OCR-damaged document into a schema, and weighing an unverifiable vendor-rename claim against
otherwise clean evidence. INV-1012 is the clearest case: deterministic validation routes it
`viable` with only a warning, and the Approval Agent chooses `hold` over the unverified
`(formerly FastShip Ltd.)` alias.

The inventory database remains agent-reachable, as the assignment intends. The Approval Agent
holds `lookup_inventory` and `lookup_prior_invoice` as read-only tools, their results are recorded
in the trace, and the enhanced high-value gate requires one inventory lookup per canonical SKU
before an invoice above USD 10,000 can be approved. Tool results can invalidate an approval; they
never substitute for mandatory validation.

## How the supplied payment stub is used

The assignment supplies `mock_payment(vendor, amount)` as a printing stub under Provided
Resources, and it is called rather than replaced. It lives in `mock_bank.py` so the seam stays
visible: that module is the only thing that pretends to move money, while `db.py` owns the record
of having done so.

Ordering is the whole point. `record_payment` opens a transaction with `BEGIN IMMEDIATE`, checks
the idempotency key, inserts, and commits. Only after the commit returns does it call
`mock_payment`. A duplicate returns from the check before any insert, and a failed insert raises,
so neither path can reach the bank. The call's result is reported back in the `PaymentResult`
message, so it is observable in output rather than decorative.

One adaptation: the stub prints to stdout, but the CLI writes results as JSON on stdout, so a
settlement line there would corrupt that contract. It prints to stderr instead, alongside the
other operational messages.

Ledger-first ordering favours at-most-once calls for this non-failing stub. A real integration
would treat the committed record as pending work, use provider-side idempotency, and reconcile or
retry incomplete transfers; that lifecycle is outside this prototype.

## Payment ledger and idempotency

SQLite performs payment check-and-insert in a single transaction. The normalized vendor plus
invoice number forms a unique idempotency key, and a deterministic `PAY-<hash>` reference makes
retries easy to audit.

Separate source-mapping tables associate every equivalent PDF/TXT/JSON representation with the
same logical run and payment. Without them, a deduplicated group records its payment under only
the first source hash, so a second batch run recognizes one representation as paid and reprocesses
the other — which previously produced a false revision conflict and flipped a paid invoice to
`NEEDS_REVIEW`. Repeating the full batch now returns the same 16 logical results.

Inventory is read, never decremented. The assignment models invoice *payment*, not fulfillment, so
stock is a validation reference rather than a reserved resource. One consequence worth naming:
across separately approved invoices the system can authorize more units than exist, because no
invoice reserves stock against another.

## Approval controls

Approval must cite real evidence IDs. Code rejects fabricated IDs, prevents blocking evidence from
being ignored, and requires a clean approval to cite required fields, reconciled totals, supported
currency, sufficient stock, and no prior payment. Tool results appear in the trace, but tool calls
never replace mandatory validation.

Invoices over USD 10,000 face a distinct enhanced-scrutiny gate. Approval must cite the
`VAL_HIGH_VALUE` evidence, call `lookup_prior_invoice` for the exact normalized vendor and invoice
identity, call `lookup_inventory` for every canonical SKU, and receive Critic acceptance. Missing
any one of those routes the invoice to `NEEDS_REVIEW` even when the model recommends approval.

Immediately before payment, the decision guard rechecks the validation route, the approval, the
evidence integrity, Critic acceptance, and prior-payment state. The LLM never receives a payment
tool at any point.

An explicit revision marker such as `R1` always requires review until a person confirms which
version is authoritative, and can never be the first representation paid. The base invoice can
still be paid when it is processed before any conflicting history.

## Other policy choices

Payment-term checks allow a three-day calendar tolerance before emitting a due-date warning, which
avoids noise from minor billing conventions while retaining material mismatches. They do not
compare invoice dates against the current date.

Normalization is intentionally conservative. `Widget A` resolves to `WidgetA` by removing spacing
and parenthetical qualifiers, but `WidgetC` is never fuzzily coerced onto a catalog item — an
unknown SKU is reported as unknown.

## Live Grok check (2026-10-15)

Six TXT/PDF invoices were each run once with `--llm-mode=live` against `grok-4.7` from a fresh
`.runtime/`. Every outcome and canonical total matched the fixture checkpoint.

| Invoice | Format | Live outcome | Amount | What the live run exercised |
|---|---|---|---:|---|
| 1001 | TXT | `APPROVED` | USD 5,000.00 | 3 real tool calls, Critic accepted, paid |
| 1002 | TXT | `REJECTED` | USD 15,000.00 | one targeted extraction repair; insufficient stock |
| 1008 | TXT | `NEEDS_REVIEW` | USD 9,900.00 | one targeted extraction repair; unknown SKUs |
| 1010 | TXT | `APPROVED` | USD 7,185.00 | 4 real tool calls, Critic accepted, paid |
| 1012 | PDF | `NEEDS_REVIEW` | USD 9,975.00 | Approval chose `hold` over the vendor alias; Critic accepted |
| 1013 | PDF | `REJECTED` | USD 22,562.80 | arithmetic and stock failures |

Grok accepted the strict JSON-schema response format and the required tool calls, and the evidence
verifier accepted every live Approval citation. Each invoice took 18–86 seconds end to end.

One quirk: Grok sometimes lists the optional `revision` field as unresolved (1002 on the first
pass, 1008 even after repair). Unresolved fields block payment, so a clean invoice can be held for
review. That errs safe but adds noise.

**Not exercised live.** The Approval/Critic revision loop (the Critic accepted on the first pass
every time), prompt-injection documents, conflicting copies, batch mode, the Streamlit live path,
repeat runs to measure variance, and the enhanced high-value gate — every invoice above USD 10,000
in the supplied corpus is rejected on stock or arithmetic before approval is reached, so that gate
is exercised by tests rather than by the corpus.

**Known limitation.** The live client performs one tool round per agent call: it requests tools
once, executes everything the model asked for, then asks for the final structured decision. The
enhanced high-value gate expects one `lookup_inventory` per SKU plus one `lookup_prior_invoice`,
so a model that does not emit them together in a single turn will fail the gate and route to
`NEEDS_REVIEW` — safe, but stricter than intended. A bounded multi-round loop is the fix; it was
scoped out because no supplied invoice reaches that path.
