# Refactoring Roadmap

Items here are known issues with root causes identified but not yet fixed.
Per CLAUDE.md policy: every patch-only fix must land here before being shipped.
Fix in Claude Code where noted — these require proper branching and testing.


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
mis-bucketed report. Hit 2026-07-02: Paintbox pays a recurring ~$3,900/mo
American Express bill from checking, but `cc_keywords` only listed
`"BANK OF AMERICA CREDIT CARD"`; fixed for Paintbox by adding `"AMERICAN
EXPRESS"`, but the same gap exists for every other client's untracked card
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

## Open: Needs Product/Data Decision

Not code bugs — need a human decision before any code or config changes.

### Does Paintbox have an Amex card that should be reconciled on its own?
`recon_log.json` has never had an `amex` entry for Paintbox — only
`bofa_checking`, `bofa_credit`, `bofa_savings`, `payroll` — but their checking
account pays a recurring ~$3,900/mo "AMERICAN EXPRESS Bill Payment" (seen
2026-06-29). Worth asking whether there's an Amex statement that should be
reconciled as its own account, the way `bofa_credit` already is, rather than
only ever showing up as an outbound payment on the checking statement. Per
CLAUDE.md client-name governance, don't add a new `amex` account type for
Paintbox without confirming with the user first.

### 3 payroll clients still missing `payroll_key`/`payroll_format`
`fcba_academy`, `paintbox_hair_studio`, and `mp_cheng` all have
`has_payroll: true` but no `payroll_key`/`payroll_format` set (fixed for
`jojo_hair_studio` 2026-07-02; see `ADDING_NEW_CLIENT.md` step 2, added the
same day). Not a code fix — each needs its correct `payroll_format` verified
against a real ADP report for that client before setting it (guessing wrong
would silently misroute the payroll parse). `silicon_valley_west`,
`needles_studio`, and `estudillo_realty` are fine as-is — `has_payroll: false`.

---

## Closed: Fixed

- `mark_clean.py`'s `find_pending()` required an exact match against the full
  tracker key (e.g. `PAINTBOX_HAIR_STUDIO_LLC`) — fixed 2026-07-02. 5 of 9
  client keys documented in `Bookkeeping-clients/CLAUDE.md`'s client table
  (`de_anza`, `estudillo_realty`, `jojo_hair_studio`, `mp_cheng`,
  `paintbox_hair_studio`) drop the legal suffix the tracker key keeps, so
  `mark_clean.py` reported "no entry found" while listing the exact matching
  entry in the same error output. Now accepts the tracker key or an
  underscore-boundary prefix of it.

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
