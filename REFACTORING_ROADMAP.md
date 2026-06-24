# Refactoring Roadmap

Items here are known issues with root causes identified but not yet fixed.
Per CLAUDE.md policy: every patch-only fix must land here before being shipped.
Fix in Claude Code where noted — these require proper branching and testing.

---

## Open: Needs Root Cause Fix


### Amex parser: `_in_ch_section` credit-scan flag never resets
In `AmexStatementParser.parse()`, `_in_ch_section` is set to `True` when a
standalone cardholder header line is seen in the credits scan but never resets
to `False`. For the current statement format this is fine (additional-cardholder
sections come last), but if a future statement format interleaves sections or
has trailing non-cardholder content with `-$` amounts, credits could be
over-captured.

**Root cause to investigate:** Reset `_in_ch_section` when a known section
boundary keyword (e.g. `Total Fees`, `New Balance`, `Summary`) is detected, or
track end-of-section markers from the statement structure.

---

### Amex parser: `_AMEX_FEE_KEYWORDS` is a hardcoded list
Fee-type detection in `AmexStatementParser.parse()` uses a hardcoded list
(`_AMEX_FEE_KEYWORDS`). New AmEx fee names (e.g. "Cash Advance Fee",
"Foreign Transaction Fee") require a code change before they are captured
rather than showing as MISSING rows.

**Root cause to investigate:** Detect fee lines structurally — any transaction
in a "FEES" section of the statement, or any charge where the description
matches a broad `FEE` suffix pattern — instead of an exact-match list.

---

### No unit tests for Amex parser fee/credit parsing
Bugs in `AmexStatementParser` (fee lines skipped, cardholder-section credits
dropped) went undetected until they surfaced as MISSING rows in production
statements. There is no fixture-based test coverage for these paths.

**Root cause to investigate:** Add a synthetic AmEx statement fixture (fictional
data, no PII) and pytest cases covering: (1) fee lines with `Card Ending` in
description, (2) credits under additional-cardholder section headers, (3)
balance equation tie-out. Pairs with Mode H (upload real fixture to Google Drive
for manual regression testing).

---

## Closed: Fixed

- Schema `statement_types` enum removed from `clients/_schema.json` — fixed
  2026-06-24 by replacing the enum constraint with a plain `"type": "string"`.
  New parsers and cardholder subtypes no longer require a schema patch; runtime
  parser matching handles validation. Prior patch (adding 5 missing types) is
  superseded.
- Pay-by-Pay (workers comp) silently dropped from payroll JE — fixed 2026-06-24:
  `adp_payroll_departments` now extracts `DebitforPay-by-Pay` from Liability PDF in
  `parse_cash_splits()`; all three formats (`departments`, `professional`, `1099`) emit
  debit+credit rows using `workers_comp_account`/`pay_by_pay_account` config key and
  print a JE balance cross-check. Code supports both key names; rename `pay_by_pay_account`
  → `workers_comp_account` in client configs when convenient.
- Ghost row in `reconciliation_log.csv` for JoJo Citi Costco May 2026 (`total_payments = 0.00`,
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
