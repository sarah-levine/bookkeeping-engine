# Refactoring Roadmap

Items here are known issues with root causes identified but not yet fixed.
Per CLAUDE.md policy: every patch-only fix must land here before being shipped.
Fix in Claude Code where noted — these require proper branching and testing.


### `test_payroll_end_to_end.py` doesn't cover `adp_payroll_departments`/`adp_labor_distribution`
Real fixtures for both now exist (`fixture_adp_payroll_detail/liability_deanza.pdf`,
`fixture_adp_labor_distribution_duran.pdf`, added 2026-07-02, both verified
balanced via `payroll.py` directly) but aren't wired into the test. Unlike
the other four formats, `run_adp_payroll_departments()` and
`run_adp_labor_distribution()` build their journal rows inline — there's no
separate `_build_journal()` to import and call directly the way the test
does for the other formats.

**Fix:** add a harness that calls the real `run_adp_payroll_departments`/
`run_adp_labor_distribution` functions with `_qb_confirm` monkeypatched to
return `True` and `append_payroll_log`/`archive_payroll_pdf` monkeypatched
to capture args instead of writing/uploading, rather than reimplementing
their row-building logic in the test.

### BMO checking/credit card parsers never set `closing_date`/`statement_date`
`BMOCheckingParser` and `BMOCreditCardParser` never assign `self.closing_date`
or `self.statement_date` during `parse()`, same failure mode as the BofA/Wells
Fargo/Northern Trust bug fixed 2026-07-02. Any real reconciliation run through
these two parsers silently logs a blank statement date. Not fixed here — no
BMO fixture was available locally to verify a fix against. `tests/test_parsers.py`
now asserts `closing_date`/`statement_date` is set, so this will surface as a
real failure once BMO fixtures are wired into the manifest.

**Root cause fix:** add closing-date extraction to `parse()` in `parsers/bmo.py`,
following the pattern in `parsers/bofa.py`'s `_extract_closing_date()`.

### `CitiVisaCostcoParser` closing-date regex isn't OCR-noise-tolerant
Its closing-date extraction assumes clean text; OCR'd/scanned Costco Visa
statements can inject stray characters that break the regex, silently
leaving `closing_date` unset the same way the BofA bug did. Lower priority
than the BMO item — real production entries logged so far all have valid
dates, so this hasn't caused visible damage yet, just a latent risk.

**Root cause fix:** loosen the regex or fall back to a secondary date pattern
when the primary one doesn't match, rather than leaving `closing_date` as `None`.

### Vendor-normalization tests share a process-wide `ClientRegistry` singleton
`tests/test_aggregations.py` and `tests/test_vendor_normalize.py` pass in
isolation but can fail when run as part of the full `tests/` suite, depending
on run order — some other test module mutates process-global registry/config
state (tied to `BOOKKEEPING_CLIENTS_DIR`) that these tests implicitly depend
on. Made `pytest tests/` look flaky while verifying the BofA/Wells
Fargo/Northern Trust fix, though it's unrelated to that fix.

**Root cause fix:** give `ClientRegistry` proper test isolation (fixture-scoped
instance instead of a module-level singleton) rather than relying on tests
loading in a particular order.

### Check-image payee extraction is entirely manual
Nothing in the pipeline automatically reads payee names off scanned check
images (the "Check images" pages BofA and others append to checking
statements). `--check-payee`/`--check-date` exist as manual overrides, but a
human has to notice the check-images section exists, render it, and
transcribe every payee by hand — easy to miss entirely (happened
2026-07-02: check payees were reported as unavailable on a statement that
had a full check-images page later in the same PDF).

**Root cause fix:** extend the existing Vision-fallback pattern (used today
for balance tie-out recovery) to check images — detect a check-images page,
crop each check, run OCR or Claude Vision, and pre-fill `check_payee_map`
instead of requiring a human to trigger it.

### `cc_keywords` is a manually-maintained per-client list with no validation
Each client config's `cc_keywords` list is hand-maintained ad hoc; any credit
card vendor not explicitly listed silently lands in generic "Withdrawals and
Debits" instead of "Credit Card Payments" — no warning, no failure, just a
mis-bucketed report. Hit 2026-07-02: a client pays a recurring ~$3,900/mo
American Express bill from checking, but `cc_keywords` only listed their one
already-known card issuer; fixed for that client by adding the missing
keyword, but the same gap exists for every other client's untracked card
vendors and will recur the next time any client starts paying a new card.

**Root cause fix:** classify by a shared, global pattern (e.g. `<KNOWN CARD
NETWORK> ... Bill Payment` / `... Credit Card ... Payment`) as a fallback
when no client-specific `cc_keywords` match, instead of relying entirely on
each client's list staying complete.

### `adp_payroll_details.py`'s earnings-category list is a hardcoded allowlist with no fallback
Fixed the specific instance ("Sick" silently dropped from Associates gross
wages, 2026-07-02), but the pattern itself is unchanged: `parse_payroll_details()`
only sums earnings labels it explicitly regex-matches (`Regular`, `Overtime`,
`RestTime`, `Commission`, now `Sick`). Any future ADP earnings category
(`Holiday`, `Bonus`, `Vacation`, etc.) will silently vanish from `assoc_gross`
the same way, throwing the journal entry out of balance with no indication of
why. The balance check catches it (as it did here), but only after a human
has to re-derive the cause by hand.

**Root cause fix:** sum every line in the Department Totals block
generically (parse label + amount pairs, exclude only known non-wage rows
like tax/deduction columns) instead of matching an explicit allowlist of
earning-type labels, so a new ADP category degrades to "included but
unlabeled" rather than "silently missing."

---

## Open: Needs Product/Data Decision

Not code bugs — need a human decision before any code or config changes.
Which specific clients these apply to is tracked in the private
`Bookkeeping-clients` repo, not here.

### A client's checking account regularly pays a card-network bill with no corresponding reconciled account
Seen 2026-07-02: a client's `recon_log.json` has never had an entry for the
card network they pay ~$3,900/mo to from checking — only their existing
checking/credit/savings/payroll account types. Worth periodically auditing
recurring outbound card-network payments against the set of account types
actually being reconciled for that client, in case there's a statement that
should be reconciled on its own rather than only ever showing up as an
outbound payment elsewhere. Per CLAUDE.md client-name governance, never add
a new account type for a client without confirming with the user first.

---

## Closed: Fixed

- Every payroll client now has `payroll_key`/`payroll_format` set — fixed
  2026-07-02. `fcba_academy` → `adp_payroll_1099` and `mp_cheng` →
  `adp_payroll_professional` verified by running the real fixtures
  (`fixture_adp_payroll_detail_fcba.pdf`, `fixture_adp_payroll_detail.pdf`)
  through the parser and confirming a balanced journal entry.
  `paintbox_hair_studio` → `adp_payroll_tipped` verified by confirming every
  config field `adp_payroll_tipped.py` reads (`workers_comp_credit`,
  `contractor_display_name`, `departments`, etc.) was already present in
  `paintbox_hair_studio.json` — clearly written for that runner, no fixture
  needed. All three also match the legacy format-name-as-client-key mapping
  in `Bookkeeping-clients/repair_logs.py`'s `CLIENT_KEY_MAP`, an independent
  corroborating signal.

- `mark_clean.py`'s `find_pending()` required an exact match against the full
  tracker key (e.g. `ACME_SALON_LLC`) — fixed 2026-07-02. Several client keys
  documented in `Bookkeeping-clients/CLAUDE.md`'s client table drop the legal
  suffix the tracker key keeps, so `mark_clean.py` reported "no entry found"
  while listing the exact matching entry in the same error output. Now
  accepts the tracker key or an underscore-boundary prefix of it.

- `WellsFargoCheckingParser` and `NorthernTrustCheckingParser` never set
  `closing_date`/`statement_date` — fixed 2026-07-02, same bug/fix pattern as
  `parsers/bofa.py`. Found (along with the still-open BMO and Citi Costco
  issues above) by wiring `tests/fixtures_manifest.json` to `source: "repo"`
  entries pointing at real fixtures already in `Bookkeeping-clients/fixtures/`
  so `test_parsers.py` could actually run instead of skipping for lack of
  Drive credentials. Verified via `pytest tests/test_parsers.py` against real
  fixtures, not just direct parser calls.
- Payroll runs entered in QuickBooks outside a session got re-derived next
  time — fixed 2026-07-06: added `mark_payroll_done.py <client_key>
  <check_date> <bank_credit>`, parallel to `mark_clean.py` for reconciliation.
  Writes `payroll_log.csv`, `reconciliation_log.csv`, and `recon_log.json`
  straight from the check date and known bank-credit total, without
  reparsing the ADP PDFs. `client_key` accepts either a client's
  `payroll_key` or its name/canonical key/alias — all resolve through
  `ClientRegistry`'s alias map, which already registers `payroll_key`
  (fixed separately in `34262e7`). `payroll_log.csv` marks the `balanced`
  column `"N/A (marked done — not parsed from PDFs)"` since
  there's no journal-entry breakdown to cross-check without the PDFs.
  Covered by `tests/test_mark_payroll_done.py` (temp clients/logs dirs, git
  push monkeypatched out — no real client data touched). The underlying
  asymmetry (reconciliation always logs an `IN_PROGRESS` entry on parse;
  payroll logs nothing until confirmed) is unchanged — this only adds the
  retroactive escape hatch the roadmap called for.
- Schema `statement_types` enum drift blocking all clients on startup — fixed
  2026-07-06: a single client config with an unrecognized `statement_type`
  (e.g. a new parser subtype not yet added to the `clients/_schema.json`
  enum) caused `ClientRegistry._load()` to raise and abort construction
  entirely, so *no* clients loaded even though only one config was bad.
  `parsers/base.py` now warns to stderr and skips just the offending config,
  same as the existing non-dict-JSON skip path; every other client still
  loads. The enum itself is unchanged (still catches genuine typos —
  `tests/test_config_and_logs.py::test_registry_rejects_invalid_config`
  updated to assert isolation instead of a raised `ValueError`) — a brand
  new parser subtype still needs the manual enum update to be recognized,
  it just no longer takes the whole registry down in the meantime.
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
