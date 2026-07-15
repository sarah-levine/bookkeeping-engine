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

---

## Closed: Architecture Proposal — standardize the parser → report pipeline (complete, 2026-07-14)

Captured 2026-07-07 from a design discussion, not a bug. Motivation:
nearly every duplication bug found and fixed around that time
(`normalize_vendor()` x5, `credit_card_payments` classification x4, three
local `agg()` reimplementations, the balance-tolerance check) had the same
root cause — each parser owned not just PDF extraction but also its own
classification, aggregation, and report-assembly logic, hand-rolled per
bank. That was a structural invitation for the same bug shape to keep
recurring.

Rolled out across every parser with a real fixture between 2026-07-14 and
this entry — see the "Status" sections below for the play-by-play, and
"Rollout playbook" near the end for the final state (only Capital One
remains off the pipeline, blocked on a real fixture existing at all). The
proposal below is preserved as written at the time, as the historical
record of what was proposed before the rollout began.

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

### Status: Northern Trust prototype complete (2026-07-14)

The three-stage pipeline was prototyped end-to-end on
`NorthernTrustCheckingParser` (`refactor/nt-pipeline-prototype` branch, 5
commits — harness, schema, Extract, Classify, cleanup), phased more finely
than the "Recommended approach" above for lower risk: each commit left the
codebase in a fully working, byte-identical state. New pieces added:

- `tests/dump_report.py` — the byte-identical-report-diff verification tool
  this section's acceptance bar assumed but that didn't actually exist
  before now. Every past fix in the "Closed: Fixed" section below was
  verified by hand (`git stash` + manual `diff`); this makes it repeatable.
- `parsers/row_schema.py` — the standard `TransactionRow` shape. **Sign
  convention decided**: debits/fees negative, credits/payments positive
  (matches Northern Trust's own prior convention). Citi and any other
  parser that stores debits positive will need explicit inversion in its
  own Extract step when migrated.
- `parsers/classify.py` — `classify_checking_rows()`, the shared
  Classify stage, scoped to what Northern Trust actually needs (CC-payment
  classification + Square line-position remapping). Deliberately named
  specific to checking-account statements, not generic — see the module
  docstring for why generalizing further needs a second real caller first.

**Known limitation carried forward**: Northern Trust never calls
`_aggregate_by_vendor()` (no real client config it uses sets an
aggregation-relevant knob), so this prototype validates Extract + Classify
+ Report but **not** the Aggregate stage's dedup behavior. `_aggregate_by_vendor()`
in `parsers/base.py` was left untouched. Recommend **Citi Savings** as the
next parser migrated specifically to prove Aggregate for real, before
`classify.py`'s responsibilities expand to cover it.

**External-consumer constraint discovered and preserved**:
`reconcile_comprehensive.py` reads `getattr(parser, 'credit_card_payments', [])`
directly (independent of `generate_report()`'s text) to drive its
"flag unrecognized CC payment" feature. Any future parser migration must
keep this attribute populated in its existing shape
(`{date, vendor, amount}`, sign per the parser's own convention) — a
report-text diff alone won't catch a regression here, since the report can
render identically even if this attribute's shape changes. Verified for
Northern Trust via `tests/smoke_all_fixtures.py` against its one real
fixture, which exercises the real `reconcile_comprehensive.py --dry-run`
path, not just `generate_report()`.

**One real fixture is not enough to trust as a regression gate on its
own** — Northern Trust's one real fixture has zero CC-payment lines and no
Square-mapped transactions, so it can't exercise either path.
`tests/test_northern_trust_synthetic.py` was added as a belt-and-suspenders
companion (fictional client, no PDF needed) specifically to cover what the
one real fixture can't. Recommend the same treatment — a synthetic
companion test, not just the real fixture(s) — as standard practice for any
parser with only 1–2 real fixtures in `fixtures_manifest.json` (most of
them, per a count taken during this work).

### Status: Chase migration complete (2026-07-14)

`ChaseParser` (`parsers/chase.py`, all three registered formats —
`chase_ink`/`chase_sapphire`/`chase_united` share one class) migrated on
`refactor/chase-pipeline`, 3 commits (synthetic test + real-fixture
baselines, Extract + adapter, cleanup/roadmap/merge) — thinner than
Northern Trust's 5 because no new shared tooling or `classify.py` addition
was needed this time. Verified byte-identical against all 4 real fixtures
in `fixtures_manifest.json` (spanning both statement line formats and two
different client statements — better real coverage than Northern Trust's
one fixture) plus
`tests/test_chase_synthetic.py`.

Findings that matter for future migrations:

- **First real proof of the Aggregate stage surviving the pipeline.**
  Chase's `generate_report()` calls `self._aggregate_by_vendor()` on both
  purchases and interest charges — unlike Northern Trust, which never
  aggregates. `_aggregate_by_vendor()` itself was left untouched in
  `parsers/base.py`; this migration only proves rows flowing through
  `TransactionRow` still feed it correctly. The "prove Aggregate for real"
  item from the Northern Trust status above is now satisfied — Citi Savings
  no longer needs to be prioritized for that specific reason (still a
  reasonable next target on its own merits).
- **Classification can be entangled with extraction, parser-shape
  dependent.** Northern Trust's raw text prints an explicit `"ACH Debit"` /
  `"ACH Credit"` label — real extraction-time signal, letting Classify stay
  cleanly downstream. Chase's lines carry no such label at all: deciding
  payment/credit/charge requires calling `_classify_cc_transaction()`
  (`parsers/base.py`) with both vendor text *and* amount, so a row can't
  even be constructed without classifying it first. `chase.py`'s
  `_extract_rows()` calls the classifier directly, documented inline as a
  parser-shape-dependent wrinkle. **Check this on a per-parser basis** when
  migrating the rest — don't assume every parser's raw text carries an
  NT-style type label.
- **No new `parsers/classify.py` function added.** `_classify_cc_transaction()`
  was already the shared Classify building block (used by Chase and,
  per this file's history, other credit-card parsers too) — there was no
  parser-specific classification logic to peel out the way Northern Trust's
  Square-mapping was. A shared credit-card bucket-shape-adapter (the
  `description`-vs-`vendor` field-naming, the fixed `'PAYMENT - THANK YOU'`
  literal, `str(Decimal)` for charges) is a candidate for `classify.py`
  once a **second** credit-card parser is migrated and its bucket shapes
  are confirmed to actually match Chase's — not before, same
  don't-generalize-from-one-caller discipline applied to Aggregate above.
- Incidentally deduplicated: Chase's two statement-line-format branches
  (single-date/Ink, two-date/Sapphire+United) had identical copy-pasted
  classify+append logic; now both call one shared `_classify_row()` helper.

### Status: Capital One — no real fixture exists, deferred (2026-07-14)

Attempted to start this migration next per the rollout order below, but
found the gap is deeper than "missing `PARSER_MAP`/manifest wiring": **no
real Capital One statement PDF exists anywhere accessible** — not in
`fixtures_manifest.json`, not in the Drive fixtures/statements folders
(confirmed via a live Drive search for any filename containing "apital",
zero hits), and no `capital_one` entries in reconciliation-log history —
even though one client's config lists `capital_one` in its
`statement_types`. Skipped in favor of Citi Savings; the wiring gap noted
in the rollout playbook below still applies whenever a real fixture becomes
available (e.g. a scanned paper/email statement for that client).

### Status: Citi Savings migration complete (2026-07-14)

`CitiSavingsParser` (`parsers/citi.py` — `CitiCheckingParser`/
`CitiVisaCostcoParser` in the same file untouched, verified unaffected)
migrated on `refactor/citi-savings-pipeline`, 3 commits, same thin shape as
Chase's (no new shared tooling or `classify.py` addition needed). Verified
byte-identical against its one real fixture plus
`tests/test_citi_savings_synthetic.py`.

Findings:

- **Turned out architecturally closer to Northern Trust than to Chase.**
  Citi Savings' transaction lines carry an explicit type keyword (`ACH
  CREDIT`/`DEPOSIT`/`INTEREST`/etc., `ACH DEBIT`/`WITHDRAWAL`/etc., a
  `CHECK NO:` substring) — genuine extraction-time signal, no entangled
  classifier call needed the way Chase required. Confirms the roadmap
  item above ("check this on a per-parser basis") was the right caution —
  it cuts both ways, some parsers are Chase-shaped, some are NT-shaped.
- **The schema's `check` row type was used for the first time** (Northern
  Trust and Chase never needed it). Still routed into the same
  `self.withdrawals` legacy bucket as generic debits, matching current
  behavior — checks aren't a separate bucket in this parser's report.
- **One real fixture, again very thin** (a single `INTEREST` credit, zero
  withdrawals, zero checks, no combined-statement scoping exercised) —
  same synthetic-companion treatment as Northern Trust and Chase applied
  again, now clearly the norm rather than the exception for this codebase's
  fixture coverage.
- **A statement-shape wrinkle worth remembering for future migrations**:
  savings statements can arrive bundled inside a combined checking+savings
  PDF, with the checking section's balances appearing before a `SAVINGS
  ACTIVITY` marker. `statement_date`/`closing_date` are searched *unscoped*
  (can appear on an earlier page), while balances/transactions are scoped
  to start at the marker. This pre-existing scoping logic was left
  untouched and is exactly the kind of parser-specific behavior worth
  checking for before assuming a clean single-pass migration on the next
  parser.

### Status: Citi Checking migration complete (2026-07-14)

`CitiCheckingParser` (`parsers/citi.py` — `CitiSavingsParser`/
`CitiVisaCostcoParser` in the same file untouched, verified unaffected)
migrated on `refactor/citi-checking-pipeline`, 3 commits. The most
structurally complex parser migrated so far — 5 target buckets (`charges`,
`credits`, `adp_transactions`, `credit_card_payments`, `checks`) instead of
2–3 — but still no new shared tooling or `classify.py` addition needed.
Verified byte-identical against its real fixture (unusually rich — already
exercised aggregated charges, the `no_aggregate_vendors` tag mechanism via a
real client's config, checks, ADP payroll, and credit card payments) plus
`tests/test_citi_checking_synthetic.py`.

Findings:

- **A second real case of a config-dependent, vendor-mutating cascade**,
  same shape as Northern Trust's Square line-position remapping: `ACH
  DEBIT` rows run an ADP-keyword check → `no_aggregate_vendors` config tag
  (mutates the vendor string to `vendor|date`, stripped only at render time
  in `report.py`'s `_charges_section()`) → `CREDIT CRD`/`AUTOPAY` keyword
  check, in that exact order — landing in `adp_transactions`,
  `credit_card_payments`, or generic `charges` respectively. Kept
  parser-local in the adapter, same discipline as before: this vendor-
  keyword set is Citi-Checking-specific, not shared with any other parser.
- **A pre-existing, un-fixed duplication surfaced by reading this code
  closely**: the `vendor|date` tag mechanism is a *second*, independent
  "never aggregate this vendor" implementation, separate from
  `_aggregate_by_vendor()`'s own `no_aggregate_vendors`/
  `never_aggregate_vendors` handling in `parsers/base.py`. Not fixed here —
  out of scope for a byte-identical migration — but worth calling out
  explicitly as a real candidate for consolidation whenever the Aggregate
  stage itself gets generalized.
- **Vendor computation is genuinely per-type, not per-config** — worth
  noting for the next parser: `ELECTRONIC CREDIT` keeps the full, unsplit
  vendor-lookahead line while every other type splits on 2+ spaces. A
  parser-shape assumption ("all vendor text gets the same cleanup") would
  have been wrong here; check each type keyword's handling individually,
  don't assume uniformity within one parser.
- Multiple buckets can share one running total (`total_charges` here rolls
  up `charges` + `adp_transactions` + `credit_card_payments` together, not
  per-bucket) — another assumption worth checking per parser rather than
  taking for granted.

### Status: Wells Fargo Credit Card migration complete, checking parser deferred (2026-07-14)

`parsers/wells_fargo.py` has two very differently-shaped classes.
`WellsFargoCreditCardParser` (~167 lines) migrated on
`refactor/wells-fargo-credit-pipeline`, 3 commits — no new shared tooling
or `classify.py` addition needed. `WellsFargoCheckingParser` (~350+ lines)
was deliberately **not** touched this round and needs calling out
explicitly so it isn't mistaken for already covered: continuation-spanning
transactions (a single logical transaction can span multiple physical
lines, accumulated via a `flush()`-closure pattern unlike any parser
migrated so far), column positions that shift *per section header within
one statement* (not once per statement, like the credit card parser's
single `CREDIT_COL_THRESHOLD`), a bespoke ~80-line `_normalize()` override
handling Venmo/Zelle/Square-payroll pattern matching, and — critically —
its credit-card-payment/payroll classification happens
*inside `generate_report()`*, not `parse()`, unlike every parser migrated
so far. That last point means a byte-identical migration of this class
can only touch `parse()`'s credits/debits/checks/bank_fees split;
`generate_report()`'s own re-classification of `self.debits` must stay
untouched to preserve the "Report is never touched" invariant every prior
migration has relied on. Left as its own dedicated future migration.

Findings from the credit card parser:

- **A fourth distinct classification-signal shape**: purely geometric
  (`is_credit_col = len(line.rstrip('\n')) <= CREDIT_COL_THRESHOLD`, raw
  line length against a dynamically-detected column position) — no vendor-
  text inspection needed at all for the primary credit/charge split. More
  "extraction-native" than even Northern Trust's text labels. Alongside
  NT/Citi's keyword-native signal and Chase's classifier-entangled signal,
  this is a third truly distinct shape parsers can take — reinforces
  playbook item 4 below: check each parser's own signal, don't assume any
  prior parser's shape generalizes.
- **A hardcoded (not config-driven) vendor-text business rule inside the
  credit branch** — the `ONLINE PAYMENT|PAYMENT THANK YOU` regex deciding
  payment vs. generic credit — treated the same as Citi Checking's
  hardcoded `'ADP'` check: a Classify-stage decision, adapter-local, even
  though neither is config-dependent. The distinguishing test isn't
  "is this config-driven" but "does deciding the bucket need business-rule
  vendor-text inspection, or is it inherent in the raw text's structure."
- **`normalize_vendor()` is called asymmetrically** — only for the "credit,
  not a payment" case; payments use a fixed literal description, charges
  are never normalized at parse time (that happens later, inside
  `_aggregate_by_vendor()`). Preserved exactly by keeping the call in the
  adapter at the same logical point it fired before.

### Status: WellsFargoCheckingParser's local agg() replaced with the shared aggregator (2026-07-14)

Resolved ahead of the full checking-parser migration, as a standalone
targeted fix (`fix/wells-fargo-checking-shared-aggregation`, one commit) —
not a byte-identical-preserving change like the pipeline migrations, a
deliberate behavior improvement requested explicitly: the local `agg()`
closure in `generate_report()` collapsed by vendor name alone with no
month bucketing, skipped `normalize_vendor()`, and sorted by a bare string
compare. Replaced its 4 call sites with `self._aggregate_by_vendor(...,
date_fmt='%m/%d')` — the explicit `date_fmt` matters, since this parser
stores dates as bare `MM/DD` with no year, and the shared function's
default would still parse correctly via its fallback chain but render a
spurious `/00` year suffix on every aggregated date.

**A real regression surfaced and was fixed in the same commit, not
shipped separately**: `_aggregate_by_vendor()` calls `normalize_vendor()`
internally, but this parser's `flush()` (in `parse()`) already calls it
once at parse time — so aggregated buckets now get normalized *twice*.
Traced every branch of `_normalize()` (`parsers/wells_fargo.py`) for
idempotency before making the change; empirical verification against the
real fixture caught the one branch the trace missed: the Zelle to/from
name-extraction regexes required a trailing `" on"` token that's already
been stripped by the first normalization pass, so a second pass silently
degraded `"Zelle to Jane Doe"` to the generic `"Zelle Payment"` fallback,
losing the name. Fixed by making the regex also match end-of-string
(`(?:\s+on|\s*$)`), so it's idempotent on its own output. Verified via a
byte-identical before/after diff against the real fixture
(`tests/dump_report.py`) and a new regression test
(`tests/test_wells_fargo_checking_agg_synthetic.py`) covering the name
surviving double-normalization, same-vendor-different-months now
rendering as separate rows, same-vendor-same-month still aggregating, and
no spurious year suffix.

**One residual, unverifiable risk remains**: the client-specific
`vendor_rules` config path (`_registry.normalize_vendor()`) inside
`_normalize()` — a rule's `normalize_to` output could theoretically
re-trigger its own `contains` pattern on a second pass, the same failure
shape as the Zelle bug. Not fixed preemptively (would mean auditing every
real client's configured rules, which aren't all available locally) —
if a future Wells Fargo Checking client's report shows a vendor name
degrading to something generic, check this first.

### Status: WellsFargoCheckingParser migration complete — parsers/wells_fargo.py fully closed out (2026-07-14)

Migrated on `refactor/wells-fargo-checking-pipeline`, 3 commits. The most
structurally complex parser migrated in this entire rollout — more so than
Citi Checking's cascade. With this, both classes in `parsers/wells_fargo.py`
are now on the pipeline. Verified byte-identical against the real fixture
(`tests/dump_report.py`) plus `tests/test_wells_fargo_checking_synthetic.py`.

Findings:

- **Transactions spanning multiple physical lines** — no parser migrated
  before this one had this shape. A date line opens a transaction; blank-
  amount date lines get their amount from a later continuation line;
  `flush()` fires on the *next* date line or `'Totals'`. `_extract_rows()`
  keeps this as one structurally unchanged pass (the inner `flush()`
  closure, continuation handling, and column tracking all stay together) —
  only its final action changed, from direct bucket-append to building a
  `TransactionRow`.
- **Column positions shift *within* one statement**, not just between
  statements — `dep_col`/`deb_col` are genuinely mutated mid-loop as later
  section headers are crossed. This is a stronger version of Chase's
  "can't split metadata from transaction extraction" lesson: here it's not
  just ordering that matters, it's *live mutable state* threading through
  the whole pass. Any future parser with dynamically-updating extraction
  state needs the same "don't split the loop" treatment.
- **Normalization couldn't be deferred to aggregation** (unlike Chase/Citi)
  — `checks` and `bank_fees` never go through `_aggregate_by_vendor()` at
  all (checks render individually, bank fees are summed but never
  rendered as a list), so `self._normalize()` had to stay called at parse
  time, at the exact point it fired before. A reminder that "defer
  normalization to aggregation" (the Chase/Citi pattern) is only safe when
  *every* bucket a row can land in actually goes through the aggregator —
  check this per parser before assuming it generalizes.
- **The same cleanup regex can legitimately run twice, at two different
  text states, and that's not accidental duplication** — check-payee
  cleanup runs once on raw extracted text and again inside `flush()` on
  normalized text. Preserved both call sites rather than consolidating —
  worth remembering this rollout has now found *both* directions of this
  pattern: real accidental duplication (Citi Checking's `agg()` vs the
  no-agg tag) and legitimate intentional redundancy (this one). Don't
  assume a repeated pattern is always a bug to fix.
- **`TransactionRow` has no field for "two pieces of who-information"** —
  checks here carry both a payee and a check number (unlike Citi
  Checking's checks, which have no payee at all). Resolved by repurposing
  fields the same way Citi Checking did, just inverted: `vendor` = payee,
  `raw_description` = check number. Not a schema change — the schema
  stays deliberately minimal; parser-specific two-piece data gets
  repurposed into existing slots with a clear docstring, not a new field,
  until a *third* parser needs the same shape and the pattern is proven
  common enough to formalize.

### Status: BofA Credit Card migration complete, Checking/Savings deferred (2026-07-14)

`BankOfAmericaCreditCardParser` (`parsers/bofa.py`, ~195 lines) migrated on
`refactor/bofa-credit-pipeline`, 3 commits. `BankOfAmericaCheckingParser`
(~536 lines) and `BankOfAmericaSavingsParser` (inherits `parse()` from
Checking, overrides only `generate_report()`) are deliberately **not**
touched — deferred to their own future migration, same two-step pattern
used for Wells Fargo. Verified byte-identical against both real fixtures
for this format plus `tests/test_bofa_credit_synthetic.py`.

Findings:

- **A fifth classification-signal shape: section-based document state.**
  Three mutually-exclusive states (`in_payments`/`in_charges`/
  `in_credits_section`) toggle on section-header text markers as the loop
  scans — extraction-native (no vendor-text inspection needed to know
  *which section* a line is in), structurally similar in spirit to Wells
  Fargo Checking's dynamic column tracking but signaled by text headers
  instead of geometry.
- **A hybrid case**: within the payments/credits section specifically, a
  second-level split is classifier-entangled exactly like Chase —
  `_classify_cc_transaction()` (the same shared classifier Chase and
  Capital One already use) decides payment vs. genuine credit, and can't
  be deferred to the adapter without already knowing the type. First
  parser to combine a structural first-level signal with a classifier-
  entangled second level.
- **`self.charges` has a genuinely mixed sign convention — the first
  parser in this rollout where the usual "force negative, `abs()` back in
  the adapter" pattern would have been actively wrong**, not just
  unnecessary. `self.payments`/`self.credits` always store `abs(amount)`,
  but `self.charges` stores the *raw signed* regex match with zero
  forcing. Applying the usual pattern would have silently flipped
  genuinely negative charges (in-section merchant adjustments) positive —
  caught by reading the code closely before writing any migration code,
  not by a failing test. The row carries the exact raw signed amount
  through unchanged instead. **Lesson for future migrations**: check
  whether a bucket's sign convention is uniform before assuming the
  standard sign-flip pattern applies — this one wasn't, and forcing it
  would have corrupted data invisibly (the byte-identical check would
  still have caught it before merge, but it's a reminder the "just flip
  and abs()" pattern is a default, not a law).
- **A real, pre-existing bug found by reading closely, not by testing**:
  `self.fees` is used (`self.fees += ...`) but never initialized —
  logged as its own root-cause item near the top of this document (not
  fixed here, confirmed dormant against both real fixtures).

### Status: BofA Checking/Savings migration complete — parsers/bofa.py fully closed out (2026-07-14)

`BankOfAmericaCheckingParser` migrated on `refactor/bofa-checking-pipeline`,
3 commits. `BankOfAmericaSavingsParser` inherits `parse()` unchanged from
Checking (only overrides `generate_report()`), so this single migration
covers both real fixture formats — confirmed by byte-identical diffs
against all 4 real fixtures (2 checking, 2 savings). With this,
`BankOfAmericaCreditCardParser` (done previously) and
`BankOfAmericaCheckingParser`/`SavingsParser` (done here) mean
`parsers/bofa.py` is now fully on the pipeline, same milestone as
`parsers/wells_fargo.py` and `parsers/citi.py` (Visa Costco pending)
earlier in this rollout.

Findings:

- **The most intricate `parse()` migrated so far** — three-way section
  state (deposits/withdrawals/checks) combined with multi-line
  continuation accumulation via a `current_date`/`current_desc`/
  `current_amount` triple, where *every* section-transition and closing
  marker flushes the pending transaction using the state *as it was
  before* the transition — order-of-operations-critical in a way that
  compounds Chase's "can't split the loop" lesson with Wells Fargo
  Checking's "live mutable state threading through the pass" lesson.
- **Checks are parsed on a completely different code path from
  deposits/withdrawals within the same method** — no continuation
  accumulation at all, immediate single-line `re.findall` append. A
  reminder that "how transactions are parsed" isn't necessarily uniform
  across buckets *within* one parser, let alone across parsers — check
  each bucket's actual mechanics, not just the parser's as a whole.
- **Confirms the BofA-family-wide "never force a sign" convention** across
  a second class now (first seen in `BankOfAmericaCreditCardParser`'s
  charges bucket) — credits, debits, and checks all store whatever signed
  value the statement's own text used, with zero `abs()`/normalization at
  extraction time. This is now a confirmed *family* pattern, not a
  one-off — worth checking for in any future BofA-adjacent parser work.
- **A structural (not vendor-text) skip filter checks the client's own
  name appearing in a continuation line** — `if self.client_name and
  self.client_name in line: continue`. Preserved exactly; this is
  extraction-native (uses parser state, not a business-rule keyword list),
  so it stayed in `_extract_rows()`, not deferred to an adapter.
- `generate_report()` here is the most elaborate in the entire rollout
  (ADP/online-banking-transfer/CC-payment/`transaction_aggregations`
  classification, plus reconciliation-log-CSV-driven "pending" CC-payment
  tagging) — entirely untouched, confirming the "Report never touched"
  boundary holds even for the most complex case encountered.

### Status: Amex Checking migration complete, Amex (credit card) deferred until the next status entry below (2026-07-14)

`AmexCheckingParser` migrated on `refactor/amex-checking-pipeline`, 3
commits, byte-identical against its one real fixture. `AmexStatementParser`
(credit card, ~285 lines — cardholder-aware dynamic regex, two-pass
Payments/Credits-then-Charges structure, value-based dedup cleanup) is
**not** migrated in this entry — deliberately deferred in favor of the
smaller Checking class this round, per the reordering request to do Amex
before Citi Visa Costco. See "Status: Amex (credit card) migration
complete" below for its own migration, done as a separate follow-up.

Findings:

- **First parser in the rollout where `generate_report()` calls
  `self.parse()` itself** (`def generate_report(self, ...): self.parse()`),
  rather than relying on the caller having already called `parse()` once.
  This makes `parse()`'s existing idempotency-reset block (zeroing
  `self.credits`/`self.debits`/`self.checks`/`self.interest_earned`/
  balances/`statement_date`/`account_number` at the top) load-bearing in a
  way it wasn't for any prior parser — `_rows_to_legacy_shape()` appends to
  those lists, so without the reset a second `parse()` call would double
  every transaction. Preserved exactly, untouched, ahead of the new
  `_extract_rows()`/`_rows_to_legacy_shape()` calls. Covered directly by a
  synthetic test that calls `parse()` twice and asserts identical results.
- **`interest_earned` is a running scalar total, not a row-shaped bucket**
  — interest-deposit lines never appear as individual line items in the
  rendered report, only accumulate into `self.interest_earned`. Treated the
  same as `self.service_fees`/`self.finance_charge` in other parsers:
  accumulated as a direct side effect inside `_extract_rows()`, never
  converted to a `TransactionRow`. This is a scoping decision about what
  counts as "a row" at all, not a schema change — worth checking for on
  future parsers with a similar "summary total, not a transaction" field.
- **Classification is extraction-native, no classifier entanglement** —
  `is_credit`/`is_debit` come directly from text labels literally printed
  on the transaction line (`': CREDIT'`, `': DEBIT'`, `'INTEREST DEPOSIT'`,
  `'WIRE TRANSFER DOMESTIC INCOMING'`, `'CHECK: WITHDRAWAL'`), the same
  NT/Citi-style shape, not Chase's.
- **Sign convention is the Citi/Chase-family "always positive, direction =
  bucket membership"** — not the BofA family's "preserve raw sign."
  Standard sign-flip-then-`abs()`-in-adapter pattern, no special handling.
- A post-loop cleanup filter (`self.debits = [d for d in self.debits if
  'CHECK' not in d['description'].upper()]`) removes check-labeled debits
  that would otherwise double-count against the `Checks Paid Summary`
  section — a batch cleanup over the already-built legacy list, so it
  stayed in `parse()`, running after `_rows_to_legacy_shape()`, untouched.

### Status: Amex (credit card) migration complete — parsers/amex.py fully closed out (2026-07-14)

`AmexStatementParser` migrated on `refactor/amex-statement-pipeline`, 3
commits, byte-identical against all 4 real fixtures (`amex_duran`,
`amex_fcba`, `amex_silicon_valley_west_annual_fee`,
`amex_silicon_valley_west_late_fee` — the most of any parser in this
rollout except Chase). With `AmexCheckingParser` (done in the entry above)
also migrated, `parsers/amex.py` is now fully on the pipeline, same
milestone as `parsers/wells_fargo.py` and `parsers/bofa.py`.

Findings:

- **Two independent sequential passes over `lines`, not one combined state
  machine** — the first parser in this rollout with that shape. A
  Payments/Credits pass scans *all* of `lines`; a separately-scoped Charges
  pass scans only `lines[charges_start:]` (from the standalone `"New
  Charges"` header onward), deliberately bounded because the earlier
  Payments/Credits block has an inline-cardholder-prefix shape that would
  mis-split under the Charges pass's trailing-junk stripper. `_extract_rows()`
  keeps both as separate sub-passes rather than fusing them into one state
  machine — they scan different line ranges for different reasons, and
  fusing them would have obscured that the first pass is (today,
  unintentionally — see the new "Open" item above) unscoped.
- **The Charges pass is classifier-entangled** (calls
  `self.normalize_vendor()` then `_classify_cc_transaction()` to decide
  credit vs. charge), same shape as Chase and
  `BankOfAmericaCreditCardParser`'s payments section — classification
  happens in `_extract_rows()`, can't be deferred to the adapter. The
  Payments/Credits pass, by contrast, is extraction-native (payment
  keywords / negative-amount sign alone decide type, no classifier call) —
  a reminder that a single parser can mix both shapes across its own
  different sections, not just across different parsers.
- **`cardholder` needed a `TransactionRow` overflow slot** — `self.charges`
  carries a `cardholder` field (set by standalone cardholder-name header
  lines in the Charges section, persisting via `current_cardholder` state
  until the next header) that `TransactionRow` has no dedicated slot for.
  Reused `raw_description` for it, the same overflow-slot precedent used
  for check numbers in Citi/Wells Fargo/BofA Checking.
- **Confirmed the Citi/Chase-family "always positive" sign convention** for
  the charges bucket — *not* `BankOfAmericaCreditCardParser`'s mixed-sign
  convention, despite both being credit-card "charges" buckets. Amex's
  Charges pass explicitly routes any negative amount to `self.credits`
  before it can reach `self.charges`, so the bucket is always positive by
  construction; BofA's charges section can contain an un-flagged negative
  adjustment line with no such routing. A reminder that "charges bucket"
  isn't itself a fixed sign convention across parsers — check each one.
- **Discovered (not introduced, not fixed) a pre-existing double-counting
  bug**: the unscoped Payments/Credits pass and the scoped Charges pass
  both independently match a negative-amount line inside the Charges
  section, producing the same credit twice. Found via synthetic test
  construction (none of the 4 real fixtures happen to contain such a line)
  — logged above under "Open: Needs Root Cause Fix", preserved
  byte-identically by this migration since both passes still scan exactly
  the same lines they did before.
- **All 3 real fixture clients (`duran`, `fcba`, `silicon valley west`)
  happen to have cardholders configured** — none exercises the
  no-cardholders-configured fallback (`cardholder_pattern = None`,
  `_cardholder_inner = '(?!)'`). Added synthetic coverage for it
  specifically, since it's likely the *more* common case across the rest
  of the client base, not an edge case.

### Status: Citi Visa Costco migration complete — Architecture Proposal rollout complete (2026-07-14)

`CitiVisaCostcoParser` migrated on `refactor/citi-visa-costco-pipeline`, 3
commits. This was the last parser on the rollout: `parsers/citi.py`,
`parsers/wells_fargo.py`, `parsers/bofa.py`, and `parsers/amex.py` are now
all fully on the Extract/Classify/Report pipeline. **Capital One** remains
the sole holdout, deferred indefinitely pending a real fixture (see status
above) — not part of "complete" above, but not schedulable either.

While scoping this migration, found and fixed **separately** (own branch,
own commit, merged to `main` ahead of the pipeline work) a real crash bug:
`parsers/citi.py` never imported `_classify_cc_transaction`, so
`_store_transaction()` raised `NameError` on any transaction line that
matched far enough to reach it. Confirmed two historical `recon_log.json`
entries (both logged `CLEAN`, `$0.00` difference) are consistent with this
bug going undetected — the `_add_missing_row()` balance-padding mechanism
makes an empty parse look reconciled regardless of whether real line items
were ever captured. Re-running reconciliation for those two statements
against the fixed parser was deferred as a separate follow-up task, not
folded into either the patch or this migration, per the user's direction —
re-processing real client statements is a Mode D/E workflow action, not a
code-patch or refactor side effect.

Findings:

- **The real fixture provides almost no coverage of the transaction-
  extraction loop itself** — the inverse of every other migration in this
  rollout. `citi_visa_costco_jojo` is OCR-garbled badly enough that no line
  ever reaches the transaction-classification call site, before or after
  this migration (confirmed both ways via `tests/dump_report.py`, same
  near-empty output each time). The 14-test synthetic suite added in
  Commit 1 was the *primary* verification here, not a gap-filler — worth
  remembering for any future parser whose only real fixture is a heavily
  scanned/OCR'd statement.
- **One long, single-pass, deeply stateful loop with 4 fallback shapes** —
  more branches than any prior parser (inline date+vendor+amount,
  amount-only continuation, vendor+amount-continuation-with-no-date, and a
  malformed-partial-date payment fallback), all sharing
  `pending_date`/`pending_vendor` state. Stayed as one sequential scan in
  `_extract_rows()`, same category as BofA Checking's continuation
  accumulation and Amex Statement's Charges pass — just with more
  branches to keep intact.
- **A classifier-entangled helper called from 4 separate sites** —
  `_store_transaction()` was called from all 4 fallback branches, not just
  one. Converted into a nested closure (`_make_row()`) inside
  `_extract_rows()` rather than an instance method, so all 4 call sites
  stayed textually identical (`_make_row(...)` in place of
  `self._store_transaction(...)`) instead of needing four different
  row-building patterns inlined separately.
- **Heavy OCR-repair preprocessing folded into the Extract stage** —
  `fix_ocr_line()`'s ~14 chained regex substitutions are pure input
  transformation with no business logic, and nothing outside the
  transaction loop consumes the OCR-fixed lines (metadata/balance
  extraction upstream reads `raw_lines` directly) — so it moved into
  `_extract_rows()` wholesale, unchanged.
- **No balance-verification block at all** — unlike every other parser in
  this rollout, `generate_report()` here has no `_balance_check`/
  `_is_balanced` call, confirmed directly (no "Balance verification:
  PASSED" line ever prints). The synthetic test asserts on the rendered
  summary figures instead, since there's no pass/fail line to check.
- **Sign convention is the Citi/Chase-family "always positive"** — every
  bucket stores `abs(amount)` regardless of type, consistent with
  `CitiCheckingParser`/`CitiSavingsParser` (already migrated earlier in
  this rollout) and unlike BofA's mixed-sign charges bucket.

### Rollout playbook

**Nothing scheduled.** The Architecture Proposal rollout is complete —
every parser with a real fixture is on the Extract/Classify/Report
pipeline. Only **Capital One** remains off it, blocked on a real fixture
existing anywhere accessible (manifest, Drive, reconciliation-log
history) — confirmed absent as of this writing (see status above). If one
becomes available, the same 3-commit branch pattern applies:

1. Same acceptance bar as every migration in this rollout, using
   `tests/dump_report.py`: byte-identical `generate_report()` output on
   every real fixture that parser has, plus a synthetic companion test for
   anything the real fixture(s) can't exercise. If the only real fixture
   turns out to be OCR-garbled/scanned (as happened with Citi Visa
   Costco), expect synthetic tests to carry most of the verification
   weight, not just fill narrow gaps.
2. Each migration gets its own branch, following the same
   one-branch/multi-phase-commit pattern as every parser in this rollout —
   never two feature branches open at once, per `CLAUDE.md`'s
   branch-hygiene rule.
3. Before assuming a parser's Extract stage can defer classification the
   way Northern Trust's, Citi Savings', and Citi Checking's did, check
   whether its raw statement text actually carries an independent
   type-label signal — Chase's didn't, and that changed the shape of its
   migration (see "Status: Chase migration complete" above). Check this per
   parser, not by analogy to the last one migrated.
4. Config-dependent, vendor-mutating cascades (Northern Trust's Square
   remapping, Citi Checking's ADP/no-agg-tag/CC-payment dispatch) go in the
   adapter, parser-local, no new `classify.py` function — until a second
   parser is found that genuinely shares the *same* keyword set/logic, not
   just the same shape of problem.
5. `cc_keywords` vs `cc_payment_vendors` naming stays unresolved — Northern
   Trust's `or`-fallback (`config.get('cc_keywords', []) or
   config.get('cc_payment_vendors', [])`) was preserved as-is in
   `classify.py` rather than picking one canonical name. Resolve this once
   `classify.py` has more than one caller, not with a sample size of one.

---

## Closed: Fixed

- `AmexStatementParser.parse()` double-counted a negative-amount line
  inside the Charges section — fixed 2026-07-14. The Payments & Credits
  pass scanned *all* of `lines` unconditionally, with no awareness of where
  the Charges section starts, so a genuine in-section credit adjustment
  (a negative `-$` amount, matched by `_credit_re` just as readily as any
  Payments-section line) got matched twice: once by the unscoped
  Payments/Credits pass, and again by the Charges pass. Fixed by computing
  `charges_start` once (moved earlier, shared by both passes) and bounding
  the Payments/Credits pass to `lines[:charges_start]`, falling back to the
  whole list if no `New Charges` header is found — the same tolerance the
  Charges pass already had for its own boundary. Found 2026-07-14 while
  migrating this parser to the Extract/Classify pipeline (constructing
  synthetic test coverage; none of the 4 real fixtures happen to contain a
  negative-amount line inside `New Charges`, so all 4 remain byte-identical
  after this fix). Updated
  `tests/test_amex_statement_synthetic.py::test_negative_amount_in_charges_section_routes_to_credit_not_charge`
  (now asserts exactly 1 match, not 2) and `test_bucket_counts_exhaustive`
  (credits 3, not 4). Per `CLAUDE.md`'s patch-fix log scan: `recon_log.json`'s
  11 `amex` entries all show `$0.00` difference, but that's not conclusive
  either way — the bug inflates `total_credits` and the `_add_missing_row()`
  padding symmetrically, so a balanced top-level difference doesn't rule
  out any of these having been affected. Flagged to the user, not corrected
  — resolving it would require re-parsing the historical statements, which
  the log alone can't support.

- `BankOfAmericaCreditCardParser.parse()` crashed on a real fee line —
  fixed 2026-07-14. `self.fees` was never initialized anywhere except
  inside the separate, conditionally-invoked `_try_recover_balance()`
  recovery path (only reached at `generate_report()` time, and only when
  the statement doesn't already tie out). Any real statement containing a
  `LATE PAYMENT FEE`, `RETURNED PAYMENT FEE`, or `ANNUAL FEE` line raised
  `AttributeError` the moment `parse()` reached it. Fixed by adding
  `self.fees = Decimal('0')` to `__init__`, matching every other attribute
  already initialized there. Found 2026-07-14 while migrating this parser
  to the Extract/Classify pipeline; confirmed dormant against both real
  fixtures (neither contains any of the three trigger phrases), so both
  remain byte-identical after this fix. Added a regression test
  (`tests/test_bofa_credit_synthetic.py::test_late_payment_fee_line_adds_to_fees_without_crashing`),
  since neither real fixture exercises this path. Per `CLAUDE.md`'s
  patch-fix log scan: `recon_log.json`'s 3 `bofa_credit` entries are all
  `CLEAN`/`DONE` with `$0.00` difference — consistent with the crash never
  having fired for these statements (a crash would prevent the run from
  completing at all, not produce a silently-wrong log entry), so nothing
  to correct there.

- `NorthernTrustCheckingParser` had zero CC-payment classification at all —
  fixed 2026-07-07. Added the same `_is_known_cc_network_payment()` +
  client `cc_keywords`/`cc_payment_vendors` fallback pattern used by every
  other checking parser (BofA/Amex/Wells Fargo/US Bank), splitting matched
  debits into a new `self.credit_card_payments` list (excluded from the
  generic debits bucket, matching every other parser's convention) and
  rendering them in their own "CREDIT CARD PAYMENTS" report section via the
  already-shared `_cc_payments_section()` helper. Also installed
  `pytesseract` in this sandbox (the `tesseract` binary was already
  present, just not its Python wrapper) — Northern Trust genuinely
  requires OCR with no pdftotext fallback, so this was previously
  completely untestable here; `tests/test_parsers.py`'s Northern Trust
  case now runs instead of skipping (158 → previously 157 passed/1
  skipped). Verified live against the one real Northern Trust fixture that
  exists anywhere (Needles Studio's, checked both the Drive test-fixtures
  folder and the real statement archive — nothing else exists): report is
  byte-identical except the timestamp, since that statement happens to
  contain zero CC-payment lines — confirms no regression but can't itself
  prove the new classification fires on real data. Added a synthetic test
  (`tests/test_cc_payment_classification.py::NorthernTrustCCClassificationTest`)
  reproducing the parser's real transaction-block text shape to prove the
  classification and section-separation actually work.

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

  **Update 2026-07-07:** real fixtures for both formats are now committed
  to `Bookkeeping-clients/fixtures/` (`fixture_adp_payroll_detail_deanza.pdf`
  + `fixture_adp_payroll_liability_deanza.pdf` for De Anza;
  `fixture_adp_labor_distribution_duran.pdf` for Duran HCP) and wired into
  the local, gitignored `tests/payroll_fixtures_manifest.json`. The
  synthetic-only limitation above no longer applies —
  `test_payroll_end_to_end.py` now runs all three against real data:
  `adp_payroll_departments_sample` (De Anza, 14-row journal, BALANCED
  $26,691.94), `adp_labor_distribution_agency_sample` (Duran Div 50,
  BALANCED $30,274.20), `adp_labor_distribution_admin_sample` (Duran Div
  10, BALANCED $4,140.38) — 7/7 payroll fixtures passing, 0 skipped.
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
