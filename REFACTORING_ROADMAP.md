# Refactoring Roadmap

Items here are known issues with root causes identified but not yet fixed.
Per CLAUDE.md policy: every patch-only fix must land here before being shipped.
Fix in Claude Code where noted — these require proper branching and testing.

---

## Open: Needs Root Cause Fix

*(none)*

### 6. Pay-by-Pay (Workers Comp) silently dropped from payroll JE in 3 formats
**Root cause:** Three payroll formats never read or write Pay-by-Pay from the
Liability PDF, so workers comp is silently omitted from the journal entry with
no warning. The JE still balances internally because Pay-by-Pay is an extra ADP
debit that doesn't touch the detail PDF totals — making it easy to miss in review.

**Affected formats:**
- `adp_payroll_departments` (`payroll_clients/adp_payroll_departments.py`) —
  `parse_cash_splits()` only extracts `DebitforTaxes` via regex; it never reads
  `DebitforPay-by-Pay`. No debit row for workers comp is built in
  `run_adp_payroll_departments()`. **Confirmed missing** for De Anza 5/15/2026
  ($776.56 dropped).
- `adp_payroll_professional` (`payroll_clients/adp_payroll_professional.py`) —
  no Pay-by-Pay parsing or row at all.
- `adp_payroll_1099` (`payroll_clients/adp_payroll_1099.py`) —
  no Pay-by-Pay parsing or row at all.

**Working formats (for reference):**
- `adp_payroll_details` — reads Pay-by-Pay via `parse_liability()` and writes
  a debit row using `cfg["workers_comp_account"]`.
- `adp_payroll_tipped` — reads Pay-by-Pay via `--pay-by-pay` override flag and
  `cfg["workers_comp_account"]`.

**Secondary issue — inconsistent config key name:**
`adp_payroll_details` and `adp_payroll_tipped` read `cfg["workers_comp_account"]`.
De Anza's config was added today as `pay_by_pay_account`. These should be the
same key. Pick one (`workers_comp_account` is already used by 2 clients and 2
formats) and standardize across all configs and formats.

**Fix (apply to all 3 broken formats):**

1. In `parse_cash_splits()` (or equivalent), add extraction of Pay-by-Pay:
```python
m = re.search(r'DebitforPay-by-Pay[^$]*\$([\\d,]+\\.\\d{2})', norm)
if m: result['pay_by_pay'] = amt(m.group(1))
```
2. In the JE build section, after the other debit rows, add:
```python
wc = cash.get('pay_by_pay', 0)
wc_account = cfg.get('workers_comp_account') or cfg.get('pay_by_pay_account')
if wc > 0 and wc_account:
    rows.append(make_row(check_date, wc_account, debit=wc, memo='ADP Pay-by-Pay (Workers Comp)'))
    rows.append(make_row(check_date, cfg['bank_account'], credit=wc, memo='ADP Pay-by-Pay (Workers Comp)'))
elif wc > 0:
    print(f'⚠️  Pay-by-Pay ${wc:,.2f} found in Liability PDF but no workers_comp_account in config — not included in JE')
```
3. Add a cross-check at the end that compares total JE credits against total ADP
   debited from the Liability PDF, and prints a warning if they don't reconcile.
4. Rename `pay_by_pay_account` in `de_anza.json` to `workers_comp_account` to
   match the standard key used everywhere else.
**Fix in Claude Code.**

---

## Closed: Fixed

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
