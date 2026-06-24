# Refactoring Roadmap

Items here are known issues with root causes identified but not yet fixed.
Per CLAUDE.md policy: every patch-only fix must land here before being shipped.
Fix in Claude Code where noted — these require proper branching and testing.

---

## Open: Needs Root Cause Fix

### 3. Stale ghost row in `reconciliation_log.csv` for JoJo Citi Costco May 2026
**File:** `Bookkeeping-clients/reconciliation_log.csv`
**Root cause:** The timed-out `reconcile_comprehensive.py` run early in the session
wrote a row for `citi_visa_costco / 05/20/26` with `total_payments = 0.00` and
no `account_ending`. The correct row (written later) has `total_payments = 5316.23`
and `account_ending = 3003`. The ghost row is harmless now (string sort picks
the June date) but will cause confusion on future audits.
**Fix:** Delete the ghost row in `Bookkeeping-clients` — keep only the row with
`account_ending = 3003` and correct `total_payments`.

### 4. `detect_statement_type()` OCR fallback missing `bmo_credit` branch
**File:** `reconcile_comprehensive.py` — `detect_statement_type()`, ~line 295
**Root cause:** The OCR fallback block (for image-only PDFs that return empty
from `pdftotext`) only checks for `bmo_checking` keywords (`MONTHLY ACTIVITY
DETAILS`, `BEGINNING BALANCE`). It never checks for BMO credit card keywords, so
scanned BMO CC statements always return `unknown` and are skipped — even though
`BMOCreditCardParser` now exists and the text-based detection block (~line 251)
is correct.
**Fix:** Add a `bmo_credit` branch to the OCR fallback block, before the
`bmo_checking` branch, keying on `BUSINESS PLATINUM`, `PLATINUM REWARDS`,
`REWARDS CREDIT CARD`, or `INDIVIDUAL BILL ACCOUNT SUMMARY`. Guard with
`MONTHLY ACTIVITY DETAILS not in ocr` to avoid misclassifying checking statements:

```python
if (('BMO' in ocr or 'PMO' in ocr) and
        ('BUSINESS PLATINUM' in ocr or 'PLATINUM REWARDS' in ocr
         or 'REWARDS CREDIT CARD' in ocr or 'INDIVIDUAL BILL ACCOUNT' in ocr)):
    if 'MONTHLY ACTIVITY DETAILS' not in ocr and 'BEGINNING BALANCE' not in ocr:
        return 'bmo_credit'
```
**Fix in Claude Code.**

### 5. `BMOCreditCardParser._extract_text()` uses `pdf_utils.pdf_to_text` (pdftoppm) — times out on scanned PDFs
**File:** `parsers/bmo.py` — `BMOCreditCardParser._extract_text()`
**Root cause:** `_extract_text()` calls `pdf_utils.pdf_to_text()`, which runs
`pdftoppm -r 300` on every page then pipes to tesseract. On a 2-page scanned
PDF this takes 60–120+ seconds and times out. `BMOCheckingParser` (same file)
already solves this with PyMuPDF + pytesseract directly — faster and already
installed. `BMOCreditCardParser` should use the same approach.
**Fix:** Replace the `_extract_text()` body with the PyMuPDF+pytesseract pattern
already used by `BMOCheckingParser`:

```python
def _extract_text(self):
    # Try pdftotext first (fast, works for digital PDFs)
    try:
        result = subprocess.run(
            ['pdftotext', '-layout', str(self.pdf_path), '-'],
            capture_output=True, text=True, check=True
        )
        if result.stdout.strip():
            return result.stdout
    except Exception:
        pass
    # OCR fallback via PyMuPDF + pytesseract (scanned PDFs)
    try:
        import fitz
        import pytesseract
        from PIL import Image
        doc = fitz.open(str(self.pdf_path))
        texts = []
        for page in doc:
            mat = fitz.Matrix(2.0, 2.0)
            pix = page.get_pixmap(matrix=mat)
            img = Image.frombytes('RGB', [pix.width, pix.height], pix.samples)
            texts.append(pytesseract.image_to_string(img))
        doc.close()
        return '\n'.join(texts)
    except Exception:
        return ''
```
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
