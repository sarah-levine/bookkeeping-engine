# Refactoring Roadmap

Items here are known issues with root causes identified but not yet fixed.
Per CLAUDE.md policy: every patch-only fix must land here before being shipped.
Fix in Claude Code where noted — these require proper branching and testing.

---

## Open: Needs Root Cause Fix

### BMO checking/credit parsers never set `closing_date`/`statement_date`
`BMOCheckingParser` and `BMOCreditCardParser` never assign `self.closing_date`
or `self.statement_date` during `parse()`, unlike every other parser.
`reconcile_comprehensive.py` reads that attribute via
`getattr(parser, 'closing_date', getattr(parser, 'statement_date', ''))` to
populate `statement_end_date` in `recon_log.json`/`reconciliation_log.csv` —
so any real reconciliation run through these two parsers silently logs a
blank statement date, same failure mode as the BofA checking/savings bug
fixed 2026-07-02 in `parsers/bofa.py` (also `WellsFargoCheckingParser` and
`NorthernTrustCheckingParser`, fixed the same day once real fixture testing
was wired up — see below).

Found by inspection; not fixed here because no local or Drive fixture exists
for either BMO format to verify a fix against — `tests/test_parsers.py`
skips them silently (unconfigured Drive placeholder in the manifest). Do not
fix this blind; get a real fixture into `Bookkeeping-clients/fixtures/` (or
Drive) first, per the "never push fixes we can't test" rule below.

**Root cause fix:** add closing-date extraction to `parse()` in
`parsers/bmo.py` (`BMOCheckingParser`, `BMOCreditCardParser`), following the
pattern in `parsers/bofa.py`'s `_extract_closing_date()`.

### `CitiVisaCostcoParser` closing_date regex isn't OCR-noise-tolerant
`self.closing_date` extraction (`Billing Period: MM/DD/YY-MM/DD/YY`) requires
clean digits and fails silently on scanned/photographed statements where the
embedded text layer is OCR-garbled (e.g. one Costco Visa fixture reads
`Billing Period: O3/2O//6-O4/2dt26` — not just letter/digit confusion, an
actual dropped character, so the digit-substitution helpers already in this
file (`fix_date_token`) don't recover it either).

Confirmed via `tests/test_parsers.py::test_parser_fixture` against the local
`citi_visa_costco` fixture — still fails. Lower priority than the other
entries here: real production
`citi_visa_costco` log entries have valid dates (clean e-statements parse
fine; only scanned paper statements hit this), and the regex itself is
correct for clean text — confirmed by testing it directly against a
non-garbled `Billing Period` string.

**Root cause fix:** needs a real character-recovery strategy for the
billing-period line (not attempted here — risk of overfitting a regex to one
garbled sample), or route scanned Citi Costco statements through Vision
before this regex ever sees the text.

### Vendor-normalization tests silently depend on `BOOKKEEPING_CLIENTS_DIR` being unset
`parsers/base.py`'s `_registry = ClientRegistry()` is a **process-wide
singleton**, constructed once at import time from `get_clients_dir()`
(`BOOKKEEPING_CLIENTS_DIR` env var if set, else the bundled
`clients/example_client.json`). `tests/test_vendor_normalize.py` and
`tests/test_aggregations.py` import that same `_registry` and assert against
the fictional `ACME INC` client defined only in the bundled example config.

Run `pytest tests/` with `BOOKKEEPING_CLIENTS_DIR` pointed at the real
private clients dir (needed for `test_parsers.py`/`test_end_to_end.py` to
reach real fixtures) and those two files fail — `ACME INC` isn't a real
client, so `_registry.normalize_vendor("ACME INC", ...)` falls through to
generic cleaning instead of the example client's configured rules. This
looks like flaky test-order pollution but isn't — it's 100% determined by
whatever `BOOKKEEPING_CLIENTS_DIR` was set to when `parsers.base` was first
imported in the process. There is currently no way to run the full suite
green with real fixtures wired up.

**Root cause fix:** stop sharing one global `_registry` across both testing
modes. Either give `ClientRegistry` a way to load both the example config
and the real clients dir (merged, real takes precedence), or have
`test_vendor_normalize.py`/`test_aggregations.py` construct their own
`ClientRegistry(clients_dir=REPO_DIR / "clients")` instead of importing the
shared singleton.

### Schema `statement_types` enum drifts from actual parsers
The `clients/_schema.json` enum for `statement_types` is a manually maintained
list. Any new parser or cardholder-specific subtype (e.g. `bmo_credit_roger`)
requires a manual schema update or jsonschema validation silently skips the
entire client config, blocking all clients on startup.

**Patched:** added `bmo_savings`, `bmo_credit_roger/nicholas/peter/christopher`
to the enum on 2026-06-24.

**Root cause to investigate:** Consider removing the `enum` constraint from
`statement_types` items entirely and letting runtime parser matching handle
unknown types — the schema doesn't need to gatekeep what the parsers already
validate. Alternatively, auto-derive the enum from registered parser
`statement_type` keys at schema generation time.

---

## Closed: Fixed

- `WellsFargoCheckingParser` and `NorthernTrustCheckingParser` never set
  `closing_date`/`statement_date` — fixed 2026-07-02, same bug/fix pattern as
  `parsers/bofa.py`. Found (along with the still-open BMO and Citi Costco
  issues above) by wiring `tests/fixtures_manifest.json` to `source: "repo"`
  entries pointing at real fixtures already in `Bookkeeping-clients/fixtures/`
  so `test_parsers.py` could actually run instead of skipping for lack of
  Drive credentials. Verified via `pytest tests/test_parsers.py` against real
  fixtures, not just direct parser calls.
- Pay-by-Pay (workers comp) silently dropped from payroll JE — fixed 2026-06-24:
  `adp_payroll_departments` now extracts `DebitforPay-by-Pay` from Liability PDF in
  `parse_cash_splits()`; all three formats (`departments`, `professional`, `1099`) emit
  debit+credit rows using `workers_comp_account`/`pay_by_pay_account` config key and
  print a JE balance cross-check. Code supports both key names; rename `pay_by_pay_account`
  → `workers_comp_account` in client configs when convenient.
- Ghost row in `reconciliation_log.csv` for a Citi Costco May 2026 entry (`total_payments = 0.00`,
  no `account_ending`) — confirmed absent from Bookkeeping-clients on 2026-06-24; row was
  never written to the canonical copy, so no deletion needed.
- `citi_visa_costco → citi_costco` alias missing from `load_reconciliation_log`
  in `send_morning_digest.py` — fixed 2026-06-22 by reading `acct_type_map`
  from `sheets_config.json` instead of a hardcoded dict.
- `repository_dispatch` in `reconcile_comprehensive.py` pointed at old repo
  `sarah-levine/Bookkeeping` — fixed 2026-06-22 to use `Bookkeeping-clients/dispatches`
  with `event_type: logs-updated`.
- `manual_statement_entry.py` had no sheet sync dispatch — fixed 2026-06-22.
- `CitiVisaCostcoParser.generate_report()` not passing `statement_date` to
  `_report_header` in the `load_from_dict` path — fixed 2026-06-22.
- `citi_visa_costco` not supported in `manual_statement_entry.py` — fixed
  2026-06-22 by adding `load_from_dict` to `CitiVisaCostcoParser` and wiring
  the type into `PARSER_BY_TYPE`.
- `write_both_logs` upsert key only matched `(client, account_type)` — fixed
  2026-06-24 by adding `statement_date` to the key, matching `upsert_recon_log`.
- `manual_statement_entry.py` never wrote to logs — fixed 2026-06-24 by calling
  `write_both_logs` after `generate_report()`; also added `bmo_credit` to
  `PARSER_BY_TYPE`.
- No BMO credit card parser — fixed 2026-06-24 by adding `BMOCreditCardParser`
  to `parsers/bmo.py` with `load_from_dict()`, `parse()`, `generate_report()`,
  and `_expand_date()` (MM/DD/YYYY normalization); wired `bmo_credit` into
  `detect_statement_type()`, `STATEMENT_TYPE_LABELS`, and the parser dispatch in
  `reconcile_comprehensive.py`. Pure-Python PDF text extraction and OCR fallback
  (pdftoppm + tesseract) added in `parsers/pdf_utils.py`.
- `detect_statement_type()` OCR fallback missing `bmo_credit` branch — fixed
  2026-06-24 by adding a `bmo_credit` check before `bmo_checking` in the OCR
  fallback block, keying on `BUSINESS PLATINUM`/`PLATINUM REWARDS`/`REWARDS
  CREDIT CARD`/`INDIVIDUAL BILL ACCOUNT` with guards against checking keywords.
- `BMOCreditCardParser._extract_text()` timed out on scanned PDFs — fixed
  2026-06-24 by replacing the bare pdftotext-only fallback with the same
  PyMuPDF + pytesseract pattern used by `BMOCheckingParser`.
