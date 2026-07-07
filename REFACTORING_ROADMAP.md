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

### Northern Trust has no CC-payment classification at all
Per the `cc_keywords` item already closed below, `NorthernTrustCheckingParser`
never classifies any debit as a credit-card payment in the first place —
there's no local list to expose on `self.credit_card_payments` yet (unlike
Wells Fargo/US Bank, fixed 2026-07-07 — see Closed section). Would need
classification logic added first (matching the `_is_known_cc_network_payment()`
+ client `cc_keywords`/`cc_payment_vendors` pattern used by every other
checking parser now) before the flag mechanism could ever fire for it.

---

## Open: Architecture Proposal — standardize the parser → report pipeline

Not a bug, not started — captured 2026-07-07 from a design discussion, for
whenever there's appetite to take this on. Motivation: nearly every
duplication bug found and fixed this session (`normalize_vendor()` x5,
`credit_card_payments` classification x4, three local `agg()`
reimplementations, the balance-tolerance check) has the same root cause —
each parser owns not just PDF extraction but also its own classification,
aggregation, and report-assembly logic, hand-rolled per bank. That's a
structural invitation for the same bug shape to keep recurring.

### Proposed shape: three stages instead of one monolithic parser

1. **Extract** (parser-specific, and *only* this) — PDF text → a list of
   rows in one common shape, replacing each parser's own differently-named,
   differently-shaped attributes (`self.credits`/`self.debits`/`self.charges`/
   `self.payments`/`self.adp_transactions`/`self.credit_card_payments`).
   Proposed row schema: `{date, vendor, raw_description, amount, type}`
   where `type` is one of `credit | debit | check | payment | fee`. Amount
   sign convention needs picking and enforcing consistently — today's
   parsers disagree (BofA stores debits negative, Citi stores them
   positive), which is itself a latent source of bugs like the `abs()`
   normalization already needed in `reconcile_comprehensive.py`'s
   unrecognized-CC-payment flag.
2. **Classify + Aggregate** (one shared implementation) — takes the raw
   rows plus client config (`cc_keywords`/`cc_payment_vendors`,
   `no_aggregate_vendors`/`never_aggregate_vendors`, `payroll_vendors`,
   `transaction_aggregations`) and produces the categorized, aggregated
   buckets (payroll, CC payments, checks, other charges, credits). This is
   where `_is_known_cc_network_payment()`, `_aggregate_by_vendor()`, and
   the three duplicated local `agg()` functions all converge into one
   place.
3. **Report** (already mostly centralized via `parsers/report.py`'s section
   helpers) — renders the aggregated buckets into the printed text report.

### Why this is a different scale of change than anything done today

Every fix landed this session was either a pure extraction (behavior-
preserving by construction) or a small, individually-verifiable behavior
change. This would be a real rewrite of every parser's `generate_report()`.
A lot of genuinely parser-specific business logic currently lives *inside*
those methods, not just "which bucket does this transaction belong to" —
BofA's config-driven `transaction_aggregations` rollups, Wells Fargo's
Square/EDD/IRS payroll special-casing, US Bank's check-number-range
heuristic for payroll checks, Northern Trust's position-based Square
account mapping, Amex's fee-keyword exclusion from Finance Charges. All of
that has to be correctly reinterpreted against the new standard table —
real per-parser design work, not a mechanical move.

### Recommended approach if/when this gets picked up

- Do **not** attempt this as one big rewrite across all 9 parsers.
- Define the standard row schema first (as its own reviewable step).
- Prototype the full three-stage pipeline on **one** parser before touching
  any other — Northern Trust or Citi are likely the simplest starting
  points (fewer buckets, fewer client-config knobs than BofA/Wells Fargo).
  Prove the design holds against that parser's real fixtures before
  generalizing.
- Migrate the rest one parser at a time after that, each verified
  independently against its own real fixtures — same before/after-diff
  discipline used throughout this session.
- Acceptance bar per parser: **byte-identical printed report output** on
  every real fixture that parser has, same as every fix landed today. Not
  "the numbers are right" — the actual rendered text, since that's what
  gets pasted verbatim into bookkeeping work per `CLAUDE.md`.

---

## Closed: Fixed

- `normalize_vendor()` was duplicated 5 ways — fixed 2026-07-07, a follow-up
  to the boilerplate survey. `parsers/base.py`'s `StatementParser` already
  had a base `normalize_vendor()`, but 4 subclasses (`usbank`, `northern_trust`,
  `bmo`'s two classes) each redefined an *upgraded* copy — identical to each
  other (confirmed via checksum) — adding a `.strip()` fallback the base
  lacked. A 5th (`WellsFargoCreditCardParser`) had a copy that was pure dead
  code, byte-identical to the base's un-upgraded version. Upgraded the base
  class to include the `.strip()` fallback and deleted all 5 duplicates.
  (`WellsFargoCheckingParser`'s separate override, which delegates to its
  own `_normalize()` for Wells-specific cleanup, is genuinely different and
  was left untouched.)
  Unlike the other boilerplate items, this changes the base class every
  parser inherits from — including ones with no override at all (BofA,
  Citi, Chase, Amex, Capital One), so it needed real verification, not just
  a mechanical-extraction argument. Traced where each of those parsers
  builds its vendor string before calling `normalize_vendor()`: BofA/Citi/
  Chase/Capital One already `.strip()` (and often whitespace-collapse) the
  description upstream, so the base's new fallback is a no-op there by
  construction; Amex was the one parser with an unstripped code path
  (`vendor_raw = txn_m.group(2)`, no `.strip()`), making it the only real
  behavior-change candidate. Verified via before/after diff against all 24
  real bank/credit-card fixtures (every parser type touched this session,
  excluding payroll) — zero real differences, only the `Generated:`
  timestamp line changed anywhere.
  Found one real test coupling while doing this:
  `tests/test_bmo_credit.py::test_normalize_vendor_with_config` monkeypatched
  `parsers.bmo._registry` to inject a test config, which worked while
  `normalize_vendor()` was defined directly in `bmo.py` (bare names resolve
  via the *defining* module, not the caller's). Once the method moved to
  `parsers/base.py`, the same monkeypatch silently stopped taking effect —
  not a bug in the centralization itself, but a real consequence of it.
  Fixed by also patching `parsers.base._registry` in the test.

- Three parser `__init__` methods bypassed `super().__init__()` entirely and
  silently reimplemented the base class's body inline — fixed 2026-07-07
  (item #4 from the boilerplate survey, flagged separately from items #1-3
  as "higher risk, worth doing deliberately" since it's an actual class-
  hierarchy inconsistency, not pure copy-paste):
  - `AmexStatementParser` needed a genuinely different fix, not just adding
    `super().__init__()`: it overrode a *differently-named* method
    (`_extract_text_amex()`) instead of the base class's `_extract_text()`,
    so calling `super().__init__()` naively would have run the base class's
    generic pdftotext extraction instead of Amex's zip-of-pages format.
    Renamed `_extract_text_amex` → `_extract_text` (no external references
    existed to the old name) so it properly overrides via normal
    polymorphism, then switched `__init__` to call `super().__init__()`.
  - `NorthernTrustCheckingParser` and `BMOCheckingParser` already had
    correctly-named `_extract_text()` overrides — Python's dynamic dispatch
    means `super().__init__()` already calls the *subclass's* version via
    `self._extract_text()`, so no rename was needed for these two, just the
    `super()` switch. Kept `self._ocr_text = None` set *before* the
    `super().__init__()` call in both, since `test_parsers.py` reads that
    attribute to distinguish "OCR unavailable" from a real parse failure,
    and the base `__init__` triggers `_extract_text()` immediately.
  - `BMOCreditCardParser` was deliberately left as-is — it has a genuine
    special case (`pdf_path=None` for the `load_from_dict()` manual-entry
    path) that the base `__init__` doesn't support at all; not a
    duplication bug, a real divergence in contract.
  Verified live against both real Amex fixtures (FCBA, Duran) — reports
  byte-identical except the timestamp. Northern Trust and BMO have no
  OCR-capable fixture in this sandbox, so verified by direct construction
  instead: both classes still build correctly end-to-end (extra attributes
  present, `_ocr_text` still `None` when extraction can't run), confirmed
  identical to pre-fix behavior via `git stash` for both.

- General cross-parser boilerplate cleanup — fixed 2026-07-07, requested
  directly (centralize repeated logic into shared, generic functions rather
  than copy-pasted per-parser code):
  - `_now_pst()` was byte-for-byte identical (verified via checksum) and
    completely unused/dead code, copy-pasted into 8 parser files (bofa,
    wells_fargo, chase, citi, amex, northern_trust, usbank, bmo). Deleted
    all 8 local redefinitions — the real one already lives in `parsers/base.py`
    and is used correctly by `parsers/report.py`.
  - The OCR-availability import guard (`try: import fitz/pytesseract/PIL/io;
    OCR_AVAILABLE = True/False except ImportError: ...`) was copy-pasted
    into 7 files, but only `northern_trust.py` and `bmo.py` actually use
    fitz/pytesseract/Image anywhere — wells_fargo/usbank/chase/citi/amex had
    zero calls to any of it (confirmed `OCR_AVAILABLE` itself was never even
    read in those 5). Deleted the dead copies outright; added
    `parsers/ocr_support.py` as the one shared definition, imported by the
    two files that actually need it.
  - The balance-tolerance check (`ok = abs(calc - actual) < Decimal('0.01')`)
    was copy-pasted at 13 call sites across 9 files, including the magic
    number itself. Added `_is_balanced(calc, actual, tolerance=Decimal('0.01'))`
    to `parsers/report.py`, with `tolerance` as an explicit parameter — BMO
    uses a deliberately looser `0.05` (real, intentional difference, not a
    typo), preserved via `_is_balanced(calc, actual, tolerance=Decimal('0.05'))`
    at its two call sites rather than silently tightened to the default.
  While doing this, found and fixed two unrelated, pre-existing, real bugs
  in the same files (not something this cleanup set out to find, but
  surfaced by touching these exact import lines):
  - `parsers/northern_trust.py` and `parsers/bmo.py` both did `from
    parsers.report import *`, but Python's `import *` excludes names
    starting with underscore unless the source module defines `__all__`
    (`parsers/report.py` doesn't). Every report-section helper
    (`_report_header`, `_balance_check`, `_deposits_section`, etc.) is
    underscore-prefixed, so both modules had **none of them** — a
    `NameError` on the very first line of `generate_report()` that would
    fire on any real machine where these parsers' PDF/OCR extraction
    actually succeeds. Never caught before: Northern Trust needs OCR, which
    isn't available in this sandbox (so `generate_report()` is never
    reached here at all), and nobody had exercised BMO's checking report
    far enough to hit the specific missing calls
    (`_deposits_section`/`_adp_section`/`_checks_section`/`_individual_section`).
    Confirmed via `git stash` that this is a real, reproducible crash
    against the pre-fix code (`NameError: name '_report_header' is not
    defined`), not theoretical. Fixed both by adding explicit imports for
    the specific underscore-prefixed helpers each file actually calls,
    matching the pattern every other parser already uses.
  - Added `tests/test_report_helper_imports.py`: statically parses every
    parser file's call sites against `parsers.report`'s real helper names
    (excluding any name the file defines locally, e.g. a nested function
    that deliberately shadows a module-level helper of the same name — Wells
    Fargo does this for `_individual_section`, which is correct as-is) and
    asserts the module actually resolves each one it calls. Confirmed via
    `git stash` that this test fails against both pre-fix files and passes
    post-fix — this is now a general regression guard against the same
    `import *` gap recurring in any parser, not just the two found live.
  Verified via before/after diff against all 14 real fixtures this session
  has touched (BofA, Amex, Chase, Citi, US Bank, Wells Fargo) — every report
  is byte-identical except the `Generated:` timestamp line. Northern Trust
  and BMO can't be exercised against real fixtures in this sandbox (OCR
  unavailable), so verified synthetically instead: constructed minimal
  parser instances directly and confirmed `generate_report()` now completes
  without error and both PASSED/FAILED balance-check paths render correctly,
  including BMO's `0.05` tolerance specifically (a diff of `0.03` passes,
  matching its looser tolerance; a diff of `0.10` correctly fails).

- Extended the BofA `credit_card_payments` fix (below) to Wells Fargo and US
  Bank checking parsers — fixed 2026-07-07. Both had the same root cause
  (a CC-payment list computed but never exposed as `self.credit_card_payments`,
  so `reconcile_comprehensive.py`'s unrecognized-payment flag could never
  fire), plus their own narrower classification gaps on top:
  - `WellsFargoCheckingParser` only matched two hardcoded literal patterns
    (`'WFB Credit Card'`, `'Online Transfer to'`) — no generic fallback and
    no per-client `cc_keywords`. Widened to also check
    `_is_known_cc_network_payment()` and the client's `cc_keywords` config,
    matching BofA/Amex.
  - `USBankCheckingParser` had **zero** generic fallback at all — only a
    client's own manually-curated `cc_payment_vendors` list matched, so a
    bare "AMERICAN EXPRESS"/"CAPITAL ONE" payment for any client without
    that config populated would silently land in generic Withdrawals.
    Added `_is_known_cc_network_payment()` as an additional fallback
    alongside the existing `cc_payment_vendors` config (kept, not renamed —
    backward compatible with clients already using it, e.g. Duran HCP).
  Verified live: Duran HCP's real `usbank_checking` fixture now correctly
  flags `⚠ Unrecognized Amex payment $2,000.00 on 04/03/26 — no Amex
  statement on file (ASK CLIENT) — Not Recognized Account`, where it
  silently never had before. Confirmed zero regression via before/after
  diff against both real fixtures (Needles Studio's `wells_fargo_checking`,
  Duran HCP's `usbank_checking`) — Wells Fargo's report is byte-identical
  (no bare-network debit in that particular statement to reclassify); US
  Bank's diff shows only the new flag line, nothing else changed. Northern
  Trust has no CC-payment classification logic at all yet (separate, larger
  gap, noted as a new open item above). 6 new tests in
  `tests/test_cc_payment_classification.py` cover both parsers.

- Product decision made 2026-07-07 on the "checking account regularly pays a
  card-network bill with no corresponding reconciled account" item below:
  decided NOT to add a new tracked account for the client involved (Paintbox
  Hair Studio's recurring $3,900/mo Amex bill payment from `bofa_checking`)
  — instead, strengthen the existing unrecognized-payment flag so it keeps
  catching this every month without needing a full Amex statement/account
  setup. While implementing that, found the flag mechanism had a real gap:
  `reconcile_comprehensive.py`'s "flag unrecognized CC payments" check reads
  `getattr(parser, 'credit_card_payments', [])`, but `CitiCheckingParser` was
  the *only* parser that ever set `self.credit_card_payments` — BofA's
  checking parser computed the equivalent list purely as a local variable
  inside `aggregate_transactions()`, returned but never assigned to `self`.
  This meant the flag could never have fired for the exact Paintbox case
  that prompted this item — that `recon_log.json` entry was added manually
  by a human noticing it by eye, not by the automated check. Fixed in
  `parsers/bofa.py`: `aggregate_transactions()` now also sets
  `self.credit_card_payments` (both `BankOfAmericaCheckingParser` and
  `BankOfAmericaSavingsParser`, which inherits the same method). Also added
  `abs()` before formatting the payment amount in the flag message
  (`reconcile_comprehensive.py`) — BofA stores checking debits as negative,
  Citi as positive, and the message only needs the magnitude. Also appended
  "— Not Recognized Account" to both the Amex and Chase flag messages, per
  explicit request, so these are easy to spot/filter in `recon_log.json`
  going forward. Verified live against the real Paintbox `bofa_checking`
  statement (`PAINTBOX HAIR STUDIO LLC_bofa_checking_2026-06-30.pdf`): the
  flag now correctly fires — `⚠ Unrecognized Amex payment $3,900.00 on
  06/29/26 — no Amex statement on file (ASK CLIENT) — Not Recognized
  Account` — where it silently never had before. Confirmed no regression
  against other real BofA checking/savings fixtures (JoJo, Paintbox
  savings) — balances and reports unchanged. Wells Fargo/US Bank/Northern
  Trust have the same underlying gap, noted as a new open item above (out
  of scope here — today's incident was specifically a BofA case).
  `tests/test_cc_payment_classification.py::test_credit_card_payments_exposed_on_self`
  covers the regression.

- `payroll_clients/adp_payroll_details.py`'s Associates (dept 002) earnings
  regex (`_ASSOC_EARNINGS_LINE_RE`) was end-anchored (required the line to
  end right after the dollar amount) — introduced 2026-07-06 while fixing
  the "Sick" category drop, and merged without catching this because the
  fix's own regression test used clean, hand-built text lines. Found
  2026-07-07 running `payroll.py` end-to-end against real Drive fixtures
  (JoJo Hair Studio's 6/15/2026 and 6/30/2026 payroll PDFs, pulled fresh
  from Drive — not the older cached test fixture): pdfplumber flattens
  ADP's multi-column table into one text line per row, so a real earnings
  line has tax/deduction columns trailing on the same line (e.g. `"Regular
  130.43 $2,382.75 FEDFIT $435.38 ..."`), which an end-anchored regex never
  matches. Every Associates category came back $0, understating real
  payroll by $4,753.23 and $1,556.37 on the two runs (all of it silently
  misattributed to "out of balance" rather than a parsing failure). No
  historical `payroll_log.csv` entries existed yet for either check date,
  so no backfill was needed. Fixed by dropping the end anchor (matching
  how `_ASSOC_TIPS_RE` already worked, and how the pre-2026-07-06 code
  worked via unanchored `re.match`). Verified both real fixtures now
  balance exactly; added a regression test reproducing the real
  trailing-column line shape (`test_real_shaped_lines_with_trailing_tax_columns_still_match`).

- Two bugs found via a full regression sweep — downloaded and ran
  `reconcile_comprehensive.py --dry-run` against all 17 real bank-statement
  fixtures in the Drive test-fixtures folder (not just the 1-2 pulled per
  fix earlier), confirming nothing else broke and surfacing these two
  pre-existing issues, both fixed 2026-07-07:
  - `parsers/citi.py` stored transaction amounts as `str(amount)` instead
    of `Decimal` in four places (`CitiCheckingParser.adp_transactions`,
    `.credit_card_payments`, `.charges` in `parse()`, and
    `CitiVisaCostcoParser.charges` in both `parse()` and
    `load_from_dict()`) — inconsistent with sibling fields in the very same
    functions (`self.checks`/`self.credits`), which already stored
    `Decimal` directly. This crashed `reconcile_comprehensive.py`'s "flag
    unrecognized CC payments" check on every real Citi checking/savings
    statement with an autopay/credit-card-payment line
    (`f"${pmt['amount']:,.2f}"` raises `Unknown format code 'f' for object
    of type 'str'` on a `str`), silently swallowing the intended "ASK
    CLIENT" flag every time (caught by a broad `except`, printed as "CC
    flag check failed"). Fixed at the source in `citi.py` (drop the
    unnecessary `str()` casts) and defensively in
    `reconcile_comprehensive.py` (normalize with `Decimal(str(...))` before
    formatting, so the shared flag-check code can't crash regardless of
    which parser's data feeds it). Verified live against the real Citi
    checking/savings fixtures: the flag now correctly fires
    (`⚠ Unrecognized Chase payment $1,902.85 on 05/05/26 — no Chase
    statement on file (ASK CLIENT)`) instead of crashing.
  - `ChaseParser.generate_report()` (`parsers/chase.py`) imported
    `_balance_check` but never called it — unlike every other credit-card
    parser (BofA, Amex, Wells Fargo, Citi, Capital One), Chase
    Ink/Sapphire/United statements never printed an explicit "Balance
    verification: PASSED/FAILED" line at all. The underlying numbers
    already tied out correctly (verified against a real Chase Ink
    statement); this was a missing confirmation step, not a silent
    miscalculation. Added the same `calc`/`_balance_check()` pattern used
    by the other parsers.
  - `tests/test_citi_amount_types.py` (5 tests) and
    `tests/test_chase_balance_check.py` (2 tests) cover both regressions;
    confirmed both fail against pre-fix code.
- `adp_payroll_details.py`'s Associates (dept 002) earnings-category list
  was a hardcoded allowlist with no fallback — fixed 2026-07-07.
  `parse_payroll_details()` only summed labels it explicitly regex-matched
  (`Regular`, `Overtime`, `RestTime`, `Commission`, `Sick`), so any other
  ADP earnings category (`Holiday`, `Bonus`, `Vacation`, etc.) would
  silently vanish from `assoc_gross` the same way `Sick` did until
  2026-07-02 (a client's run came up $143.60 out of balance). Extracted the
  block into its own `parse_associates_earnings()` function and replaced
  the allowlist with a generic "Label &lt;hours&gt; $&lt;amount&gt;" line
  matcher: known labels still populate their own field, but an
  unrecognized label is now summed into `assoc["other"]` (still included in
  `assoc_gross`) with a printed note, instead of silently dropping out —
  "included but unlabeled" rather than "silently missing," per the
  root-cause fix this item called for. Tips
  (`QualifiedTipPaid*`/`NonqualifiedCredit`) stay deliberately excluded
  from the generic sum, unchanged from before — they're tracked separately
  via `totals["all_tips"]` into their own pair of journal rows. No real
  fixture exists locally for this format (`jojo_hair_studio.json` has no
  `payroll_format` set, and its manifest-listed fixture PDF isn't present
  in this checkout), so `tests/test_adp_payroll_details_earnings.py`
  verifies the extracted function directly against hand-built text
  matching the existing regexes' exact shape — 6 tests covering the known-
  category regression, an unrecognized category being included instead of
  dropped, multiple unrecognized categories, tips exclusion, and boundary
  handling.
- Check-image payee extraction was entirely manual — fixed 2026-07-07,
  **scoped to BofA checking only** (the only checking parser with any
  check-image mechanism at all; Wells Fargo/Citi/US Bank/Northern Trust
  collect no check-image data and are out of scope until a real fixture
  exists for one of them). Added `extractors/vision_helper.extract_check_payees()`
  — a Claude Vision call reusing the module's existing balance-recovery
  plumbing (`is_available()`, batching, code-fence stripping) with its own
  narrower prompt/response shape (a plain payee list, not the balance JSON).
  Wired into `BankOfAmericaCheckingParser.extract_check_payees()` as an
  **opt-in** path (`BOOKKEEPING_VISION_CHECK_PAYEES=1` — off by default,
  since it's a real per-statement Anthropic API cost); falls back to the
  existing pytesseract path on any failure or when the gate is off, so
  default behavior is byte-for-byte unchanged (verified against a real
  fixture: identical report output with the gate off).
  Also fixed a related gating bug found while live-testing this: the whole
  check-image mechanism (both old and new) was gated behind a single
  `OCR_AVAILABLE` flag requiring `fitz` **and** `pytesseract` **and**
  `PIL` — meaning Vision, which only needs `fitz`/`PIL`, was needlessly
  blocked in any environment missing `pytesseract` specifically. Split into
  `_CHECK_IMAGE_LIBS_AVAILABLE` (fitz+PIL, gates the whole function) and
  `OCR_AVAILABLE` (adds pytesseract, gates only the OCR fallback loop
  specifically).
  Live-tested against a real fixture with an actual "Check images" page:
  confirmed the gate opens, images convert correctly, and a genuine
  Anthropic API call is attempted and its result plumbed back to the right
  check — but couldn't confirm a *successful* real extraction, since this
  sandboxed environment's `ANTHROPIC_API_KEY` returns `401 invalid x-api-key`
  for direct SDK calls outside the Claude Code harness itself; the observed
  failure-and-fallback behavior (clear diagnostic printed, graceful
  degradation to blank payee since pytesseract also isn't installed here)
  matched the designed error-handling contract exactly. 16 new tests across
  `tests/test_vision_helper_check_payees.py` (the extraction function in
  isolation) and `tests/test_bofa_check_payees.py` (gate on/off, success,
  and Vision-failure-falls-back-to-OCR) cover the mocked contract.
- `CitiVisaCostcoParser`'s closing-date regex wasn't OCR-noise-tolerant —
  fixed 2026-07-06. Confirmed against a real scanned Citi Costco fixture
  that this was a live bug, not just a latent risk: the billing-period text
  OCR'd to `"Billing Period: O3/2O//6-O4/2dt26"` (a dropped digit and stray
  letters, not just simple O/l digit confusion) — completely unrecoverable
  even after adding OCR-digit tolerance to the primary regex. Added a
  fallback: the same closing date OCR'd cleanly a few lines later as
  `"$209.49 as of 04/20/26"` (from "New balance as of \<date\>"), so
  `_extract_closing_date()` now tries that when the billing-period pattern
  fails. Also made the primary pattern search the whole statement with
  whitespace collapsed (not line-by-line), since OCR can split "Billing
  Period" and its dates across a line break. Verified live: the real
  fixture now reports `Statement Period: 04/20/26` in the reconciliation
  report; full before/after report diff shows only that one line added, no
  other changes. `tests/test_citi_costco_closing_date.py` covers the clean
  regression case, whitespace/line-split noise, mild digit confusion, the
  real severely-garbled fixture text (verbatim), and the "neither pattern
  matches" case.
- `test_payroll_end_to_end.py` didn't cover `adp_payroll_departments`/
  `adp_labor_distribution` — fixed 2026-07-06. Neither format has a separate
  `_build_journal()` to call directly like the other four (their rows are
  built inline in `run_adp_payroll_departments()`/`run_adp_labor_distribution()`),
  so reimplementing that logic in the test would have duplicated it. Instead
  added runners that call the real functions with `_qb_confirm`/
  `append_payroll_log`/`append_digest_log`/`archive_payroll_pdf`/
  `load_config` monkeypatched to capture args instead of prompting/writing/
  uploading, per the root-cause fix this item called for.
  `adp_labor_distribution` logs Agency (Div 50) and Admin (Div 10) as two
  separate `payroll_log.csv` rows, so it gets two manifest entries
  (`adp_labor_distribution_agency`/`_admin`) pointing at the same PDF,
  matching production's real two-log-writes-per-run shape. No real fixtures
  for either format are available in this environment (the ones the
  original item cited aren't present in the local private-clients
  checkout), so `tests/test_adp_multi_journal_wiring.py` proves the wiring
  itself with hand-built synthetic ADP report text verified against the
  real parsing functions — runs anywhere, including CI, independent of
  fixture availability.
- `cc_keywords` was a manually-maintained per-client list with no fallback —
  fixed 2026-07-06. `parsers/bofa.py` and `parsers/amex.py`'s
  `AmexCheckingParser` each hand-rolled their own partial, mutually
  inconsistent inline pattern list (`'CITI CARD'`/`'CREDIT CARD'`/`'CITICTP'`
  in one; `'AMEX EPAYMENT'`/`'CHASE CREDIT CRD'`/`'CREDIT CARD'`/`'AUTOPAY'`
  in the other) for recognizing a checking-account debit as a credit card
  payment before falling back to a client's own `cc_keywords`. Neither
  included bare major network names — exactly the gap that let the real
  ~$3,900/mo Amex bill payment (2026-07-02) land in generic "Withdrawals and
  Debits". Consolidated into one shared `_KNOWN_CC_NETWORK_PATTERNS` list +
  `_is_known_cc_network_payment()` helper in `parsers/base.py`, now including
  `AMERICAN EXPRESS`, `CAPITAL ONE`, `DISCOVER CARD`, `BANK OF AMERICA CREDIT
  CARD`, `WELLS FARGO CARD`, and `BMO CREDIT CARD` as bare fallback matches.
  Wells Fargo/Citi/US Bank/Northern Trust checking parsers still have no
  `cc_keywords`/CC-payment classification at all — out of scope here, noted
  for a future pass. Verified live against a real BofA checking fixture:
  full before/after report diff is byte-identical (this client's existing
  transactions already matched the old generic `'CREDIT CARD'` substring, so
  the fix is purely additive — zero regression risk demonstrated on real
  production data). `tests/test_cc_payment_classification.py` covers the
  previously-supported patterns (regression), the newly-added networks, and
  confirms unrelated vendors aren't misclassified.
- `drive_archiver.py`'s `_get_service()` never actually used service-account
  credentials — fixed 2026-07-06. Found while browsing Drive fixtures: after
  successfully building `service_account.Credentials` from
  `GOOGLE_SHEETS_CREDENTIALS`/`sheets_credentials.json` (source 3), the next
  guard was `if not creds or not creds.valid`. A freshly-constructed service-
  account credential always reports `valid=False` until its first actual API
  request (google-auth fetches the token lazily), so this always fell
  through to source 4 (interactive OAuth), found no `drive_credentials.json`,
  and raised `"No Drive credentials found"` — even with perfectly good
  credentials already built. Effectively dead code for anyone relying on
  service-account auth without a `DRIVE_TOKEN_B64`/`drive_token.pickle`
  already set up. Fixed by changing that guard to `if not creds:` — by that
  point in the function, a non-`None` `creds` always means an earlier source
  already succeeded (OAuth expiry/refresh is handled separately, earlier, for
  sources 1/2). Confirmed live against the real Drive fixtures folder (listed
  real files via the service-account path with no token pickle present), and
  added `tests/test_drive_archiver_credentials.py` (mocks
  `from_service_account_info`/`build`, no real credentials or network
  needed) — reproduces the original `OSError` against pre-fix code via
  `git stash`, passes against the fix.
- Vendor-normalization tests (`tests/test_aggregations.py`,
  `tests/test_vendor_normalize.py`) failed non-deterministically depending on
  the machine running them — fixed 2026-07-06. Actual root cause was
  narrower than originally suspected: it wasn't test run order or cross-test
  mutation — `parsers.base._registry` is a module-level singleton built once
  from `log_utils.get_clients_dir()`, which prefers a private clients
  directory (`BOOKKEEPING_CLIENTS_DIR`/`~/.bookkeeping/clients`) over the
  repo-local `clients/` fallback whenever one exists. On any real
  bookkeeper's machine that private directory always exists, so the
  singleton never sees `clients/example_client.json`'s "ACME INC" at all —
  `AmexAggregation`/`BofaCheckingAggregation`/`ClientRules` deterministically
  failed there (confirmed reproducible even running each file alone), while
  passing on a fresh checkout with no private clients dir. Fixed by adding
  `tests/_registry_test_utils.py` (`install_example_registry()`/
  `restore_registry()`) — the 3 affected test classes now pin
  `parsers.base._registry` to a `ClientRegistry` scoped to the repo's own
  `clients/` dir in `setUp`/`tearDown`, instead of depending on the
  environment-dependent singleton. `test_vendor_normalize.py` also needed its
  `from parsers.base import _registry` import changed to `import parsers.base
  as base_mod` — the old static import bound the name once at module load,
  so reassigning `parsers.base._registry` in `setUp` wouldn't have been
  visible to it. Full suite now passes 69/69 (10 skipped for missing private
  fixtures), verified order-independent and repeatable across multiple runs.
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
