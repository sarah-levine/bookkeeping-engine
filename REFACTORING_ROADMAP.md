# Refactoring Roadmap

Items here are known issues with root causes identified but not yet fixed.
Per CLAUDE.md policy: every patch-only fix must land here before being shipped.
Fix in Claude Code where noted — these require proper branching and testing.

---

## Open: Needs Root Cause Fix

### 1. `write_both_logs` upsert key is wrong
**File:** `log_utils.py` — `write_both_logs()`
**Root cause:** Upserts on `(client, account_type)` only, so running two statements
for the same account type in the same session (e.g. May then June Citi Costco)
overwrites the first row instead of keeping both. The correct key is
`(client, account_type, statement_date)` — matching `upsert_recon_log`.
**Risk:** Silent data loss if two statements for the same account run in one session.
**Fix in Claude Code.**

### 2. `manual_statement_entry.py` does not write to `reconciliation_log.csv`
**File:** `manual_statement_entry.py`
**Root cause:** The scanned-PDF fallback path (`manual_statement_entry.py`) never
calls `write_both_logs` or any log writer. Log entries for JoJo Citi Costco only
exist because the timed-out `reconcile_comprehensive.py` run wrote them as
`IN_PROGRESS` rows first. If that hadn't happened, the tracker sync would have
had nothing to read.
**Fix:** After `generate_report()`, call `write_both_logs` with the statement
summary values from the JSON data dict.
**Fix in Claude Code.**

### 3. Stale ghost row in `reconciliation_log.csv` for JoJo Citi Costco May 2026
**File:** `Bookkeeping-clients/reconciliation_log.csv`
**Root cause:** The timed-out `reconcile_comprehensive.py` run early in the session
wrote a row for `citi_visa_costco / 05/20/26` with `total_payments = 0.00` and
no `account_ending`. The correct row (written later) has `total_payments = 5316.23`
and `account_ending = 3003`. The ghost row is harmless now (string sort picks
the June date) but will cause confusion on future audits.
**Fix:** Delete the ghost row — keep only the row with `account_ending = 3003`
and correct `total_payments`. Do in Claude Code alongside fix #1 so the upsert
key fix prevents this class of issue going forward.
**Fix in Claude Code.**

---

## Closed: Fixed

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
- No BMO credit card parser — fixed 2026-06-24 by adding `BMOCreditCardParser`
  to `parsers/bmo.py` with `load_from_dict()`, `parse()`, `generate_report()`,
  and `_expand_date()` (MM/DD/YYYY normalization); wired `bmo_credit` into
  `detect_statement_type()`, `STATEMENT_TYPE_LABELS`, and the parser dispatch in
  `reconcile_comprehensive.py`. Pure-Python PDF text extraction and OCR fallback
  (pdftoppm + tesseract) added in `parsers/pdf_utils.py`.
