# Bookkeeping Reconciliation Engine

Automated bank statement reconciliation pipeline — from raw PDFs to QuickBooks journal entries, a morning digest email, and a live Google Sheet tracker.

---

## Supported Statement Types

**Credit Cards**
- American Express Business Gold / Platinum
- Bank of America Business Credit Card
- BMO Business Platinum Credit Card
- Chase Ink Business
- Chase United Club / MileagePlus
- Citi Costco Anywhere Visa

**Checking**
- American Express Business Checking
- Bank of America Business Checking
- BMO Premium Business Checking
- Citi Business Checking
- Northern Trust Checking
- US Bank Business Checking
- Wells Fargo Business Checking

**Savings**
- Bank of America Business Savings
- Citi Business Savings

**Payroll**
- ADP (1099, details, tipped, departments, labor distribution, professional)

---

## Requirements

```bash
pip install PyMuPDF pytesseract --break-system-packages
apt-get install tesseract-ocr poppler-utils
```

For Vision fallback (scanned/image PDFs):
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
pip install anthropic pymupdf --break-system-packages
```

---

## Usage

```bash
# Reconcile a statement
python3 reconcile_comprehensive.py statement.pdf

# Save report to file
python3 reconcile_comprehensive.py statement.pdf output.txt

# Specify check payees manually
python3 reconcile_comprehensive.py statement.pdf --check-payee 1235='Jane Doe'

# Non-interactive (auto-answers "later" at QB prompt)
python3 reconcile_comprehensive.py statement.pdf --no-prompt

# Dry run (parse + balance check, no log writes or uploads)
python3 reconcile_comprehensive.py statement.pdf --dry-run

# From Google Drive
python3 reconcile_comprehensive.py --from-drive <drive_file_id_or_url>

# Manual entry (no PDF)
python3 reconcile_comprehensive.py --manual

# Payroll
python3 payroll.py <client_key> payroll.pdf

# Mark a statement as done after QB entry
python3 mark_clean.py <client_key> <account_type> [<statement_date>]
```

### MCP Server (Claude Desktop integration)

```bash
python3 mcp_server.py
```

Exposes tools to Claude chat via the Model Context Protocol: `reconcile`, `reconcile_from_drive`, `check_status`, `mark_done`, `open_issues`, `client_list`. Configured in Claude Desktop's `claude_desktop_config.json`.

---

## Workflow Modes

When you hand a statement (or a request) to the assistant in chat, it picks one of these modes based on what's attached and what you ask for:

| What's attached / requested | Mode |
|---|---|
| Credit card statement PDF only | **A — Reconciliation** |
| Checking account statement PDF | **A → then auto-run C and E** |
| ADP Payroll Details PDF only | **B — Payroll Journal Entry** |
| Payroll PDF + checking account statement PDF | **C — Payroll + Tie-Out** |
| "Cross-check payroll" with no new PDF (prior session) | **C — Payroll + Tie-Out (log mode)** |
| "Does the CC payment tie out" / checking vs CC | **E — CC Payment Tie-Out** |
| QuickBooks Reconcile screenshots | **D — QA Verification** |
| Scanned PDF / "enter manually" | **G — Manual Statement Entry** |
| "Add new client" / new client onboarding | **F — Add New Client** |
| "Upload fixture" / new client or account type with no test file | **H — Upload Test Fixture** |

If it's ambiguous which mode applies, the assistant asks before proceeding.

```
 What's attached / asked?
 ├─ CC statement ───────────────────────────▶  MODE A  (Reconciliation)
 │                                                 │
 │                                     scanned/unparseable?
 │                                                 ▼
 │                                             MODE G  (Manual Entry)
 │
 ├─ Checking statement ─────────────────────▶  MODE A
 │                                                 │
 │                                                 ├──▶ MODE C  (payroll tie-out, from log)
 │                                                 └──▶ MODE E  (CC payment tie-out, from log)
 │                                                 │
 │                                          one combined block:
 │                                     RECONCILIATION + PAYROLL TIE-OUT + CC TIE-OUT
 │
 ├─ Payroll PDF only ────────────────────────▶  MODE B  (Payroll Journal Entry)
 │
 ├─ Payroll PDF + checking statement ───────▶  MODE C  (Payroll + Tie-Out)
 │
 ├─ QuickBooks Reconcile screenshots ───────▶  MODE D  (QA vs QuickBooks)
 │
 ├─ "Does the CC payment tie out?" ─────────▶  MODE E  (CC Payment Tie-Out)
 │
 ├─ "Add new client" ────────────────────────▶  MODE F  (Add New Client)
 │
 └─ "Upload fixture" ────────────────────────▶  MODE H  (Upload Test Fixture)

 Every A / C / E / G run ends the same way:
   print report verbatim ─▶ "Done in QuickBooks?"
       │                          │
       │                        done ──▶ log DONE/CLEAN, archive to Drive, sync sheet
       │                          │
       │                        later ─▶ log IN_PROGRESS, STOP and wait
       ▼
   never auto-advances to the next statement — one at a time, earliest date first
```

**A — Monthly Reconciliation.** Runs `reconcile_comprehensive.py` on the statement PDF. Prints the full report verbatim (balances, charges, payments, checks), runs the balance check, reads back any client notes, then asks whether the entry has been made in QuickBooks before logging DONE/IN_PROGRESS and archiving the PDF to Drive.

**Checking account auto-sequence.** A checking statement runs Mode A, then immediately checks for a matching payroll disbursement (Mode C) and a matching CC payment (Mode E) against already-logged data — no new PDFs needed for those two checks. All three are presented together as one block; QB confirmation happens once, after the full block.

**B — Payroll Journal Entry.** Runs `payroll.py` for the client, prints the journal entry (or two, for Labor Distribution clients — Agency first, then Admin only after Agency is confirmed in QB), and logs the run to `payroll_log.csv`.

**C — Payroll + Checking Account Tie-Out.** Confirms that a payroll disbursement (from this session's Mode B run, or from a prior logged run) matches a debit on the checking statement to the penny.

**D — QA Verification Against QuickBooks.** Builds a JSON snapshot from QB Reconcile screenshots and diffs it against the statement via `qa_reconciliation.py` to catch missing, duplicate, or mismatched entries.

**E — CC Payment Tie-Out.** Confirms a CC payment debit on the checking statement matches the payment received on the credit card statement, using already-reconciled log data for both sides.

**F — Add New Client.** Walks through onboarding: client config JSON, `sheets_config.json` cell map, `digest_config.json` tracker entry, then a test reconciliation and digest check.

**G — Manual Statement Entry.** For scanned/unparseable PDFs — values are entered by hand into `manual_statements.json` and run through `manual_statement_entry.py`, following the same report/QB-confirmation/sync flow as Mode A.

**H — Upload Test Fixture.** Archives a reconciled statement PDF to Google Drive as a fixture (via `upload_fixtures_to_drive.py`) and records it in `fixtures_manifest.json`, so future parser changes have real data to test against.

Every mode that writes data follows the same rule: an unrecognized client or account type is a hard stop (see `_assert_known_client` / `_assert_known_account_type`) — it asks for confirmation rather than guessing or silently creating a new key.

### Mode → script quick reference

| Mode | Script | QB gate? |
|---|---|---|
| **A** — Reconciliation | `reconcile_comprehensive.py <statement.pdf> [output.txt] [--dry-run] [--no-prompt] [--check-payee 1235='Jane Doe']` | Yes |
| **B** — Payroll Journal Entry | `payroll.py <client_key> <pdf> [--config client.json] [--pay-by-pay AMOUNT]` | No (logged only) |
| **C** — Payroll + Tie-Out | `reconcile_comprehensive.py <checking.pdf>` — cross-checks `payroll_log.csv`, no separate script | Yes |
| **D** — QA Verification | `qa_reconciliation.py <statement.pdf> <qb_data.json>` (or `--json '{...}'`) | No — this *is* the QB check |
| **E** — CC Payment Tie-Out | `reconcile_comprehensive.py <checking.pdf>` — cross-checks `recon_log.json`, no separate script | Yes |
| **F** — Add New Client | walkthrough: `clients/<key>.json` → `sheets_config.json` → `digest_config.json` → `reconcile_comprehensive.py` (test run) | No |
| **G** — Manual Entry | edit `manual_statements.json` by hand → `manual_statement_entry.py [--month MM-YYYY]` | Yes |
| **H** — Upload Test Fixture | `upload_fixtures_to_drive.py <entry_name> <format> <pdf_path>` or `--migrate-repo` | No |

Modes C and E don't have their own binary — both are `reconcile_comprehensive.py`'s checking-statement auto-sequence, cross-checking data that's already logged rather than parsing a new PDF.

### Scripts outside the router

Not mode-selected — these run on a schedule, or by hand, once a statement's already logged:

| Script | When | Does |
|---|---|---|
| `mark_clean.py <client_key> <account_type> [date]` | QB entry confirmed after the fact | Finds the matching `IN_PROGRESS` entry, marks it `DONE`, updates the CSV, sheet, and pushes |
| `mark_payroll_done.py <key> <check_date> <bank_credit>` | payroll bank credit confirmed later | Same idea as `mark_clean.py`, for `payroll_log.csv` |
| `send_morning_digest.py [--date YYYY-MM-DD] [--scheduled] [--cc-due]` | every morning, cron-triggered | Builds and sends the color-coded tracker digest by Gmail SMTP |
| `drive_archiver.py [--dry-run] <pdf> <client> <account_type> [date]` | called internally by Mode A/C/E/G on DONE | Uploads to `Bookkeeping/<Client>/<Account Type>/`, dedupes by filename, keeps the 2 most recent |
| `tools/pii_scan.py [--staged \| --audit]` | pre-commit hook, and before any publish | Flags real names / account numbers not in `pii_allowlist.txt` |
| `tools/backfill_status.py <old_status> <new_status>` | a status string gets renamed in code | Rewrites matching values across the private logs dir |
| `tools/dedup_recon_log.py [--apply]` | log hygiene, run by hand | Dry-run by default — shows duplicate `recon_log.json` entries before removing them |
| `alert_failure.py [subject]` | GitHub Actions `sync_tracker` workflow fails | Emails a failure alert — sender/recipient come from env, never hard-coded |

---

## Pipeline — Step by Step

What the script does from the moment you hand it a PDF:

1. **What are we working with?** — Did you pass a PDF, or use `--manual` for manual entry?
   - 1a. No PDF / `--manual` → manual entry mode
   - 1b. PDF provided → continue

2. **How many statements are in this file?** — One PDF can contain multiple statements bundled together; split them apart and label each by type.
   - 2a. Skip non-financial pages (e.g. Meevo register/inventory pages)
   - 2b. Citi bundle → split into checking + savings as two separate statements

3. For each statement found in the file:

4. **What kind of statement is this?** — Which bank and account type (checking, savings, credit card)?
   - 4a. Try reading the PDF text; match against known bank keywords in priority order
   - 4b. Can't read the text → OCR fallback (tesseract)

5. **Which parser should handle this?** — Look up the right parser class for this statement type.
   - 5a. No parser found for this type → skip with a warning

6. **Who is this statement for?** — Scan the text to identify which client this belongs to.
   - 6a. Client not recognized → prompt to set one up interactively

7. **What are the numbers?** — Extract balances, transactions, and checks from the PDF.
   - 7a. Apply the client's vendor renaming rules
   - 7b. Balances don't add up → try re-extracting using Claude Vision
   - 7c. Still doesn't balance → halt; do not log bad data

8. **Format the report** — Organize everything into a readable summary: balances, charges, payments, checks.

9. **Are there any new vendors we haven't seen before?** — Prompt to approve or rename unrecognized transaction descriptions.
   - 9a. Approved → save the rule to the client's config for next time

10. **Is the statement date what we expect?** — Check if the closing date matches the expected billing cycle.
    - 10a. Mismatch → warn, but continue

11. **Has this been entered into QuickBooks yet?** — Show the report and ask.
    - 11a. `done` → mark as DONE, update the Google Sheet
    - 11b. `later` → mark as IN_PROGRESS, skip the sheet for now
    - 11c. `--no-prompt` → auto-answers `later`

12. **Write the logs** — Save to both `reconciliation_log.csv` and `recon_log.json`, then push via GitHub REST API.
    - 12a. Unknown client → stop and ask
    - 12b. Unknown account type → stop and ask
    - 12c. Client names are normalized to canonical form before writing
    - 12d. ERROR status → writes to `recon_log.json` only, skips CSV to protect the tracker

13. **Are there any CC payments we can't explain?** — Flag any credit card payments in a checking account with no matching CC statement in this session.

14. **Archive to Google Drive** — Upload the statement PDF to `Bookkeeping/<Client>/<Account Type>/`, dedup by filename, keep only the 2 most recent per folder.

15. **Update Google Sheets** (only if answered `done`)
    - 15a. Update the tracker cell for this client/account
    - 15b. Append a row to the audit log tab

16. **Trigger the sheet sync** — Fire a GitHub Actions workflow to refresh the full Reconciliation Tracker.

---

**Next morning — `send_morning_digest.py`**

16. **What got reconciled yesterday?** — Load yesterday's log entries.

17. **Build the email** — Assemble the digest: what ran, what's still pending, and a color-coded tracker grid.
    - 17a. Green — CC reconciled, checking unblocked, all good
    - 17b. Yellow — statement available but not reconciled yet
    - 17c. Orange — CC is pending and checking is blocked
    - 17d. Pink — overdue
    - 17e. Red — ERROR (technical failure, with error detail)

18. **Send the email** — Deliver via Gmail SMTP.

---

**When QB entry is confirmed later — `mark_clean.py`**

```bash
python3 mark_clean.py <client_key> <account_type> [<statement_date>]
```

19. Find the IN_PROGRESS entry matching the client and account.
    - 19a. Not found → show what's currently pending
    - 19b. Multiple matches → ask for the date to narrow it down
20. Mark it DONE → update `recon_log.json`
21. Update `reconciliation_log.csv`
22. Update Google Sheets
23. Push to the private repo

### Module call chain

`reconcile_comprehensive.py` is the orchestrator — it never parses a statement itself. Everything statement-specific lives in `parsers/`; everything log/archive/sheet-specific lives in its own small module, each imported lazily (inside the function that needs it, not at the top of the file) so a missing credential for one integration never blocks the others.

```
reconcile_comprehensive.py
│
├─ detect_statement_type()        ~230-line, order-dependent text-sniffing chain,
│                                  kept in this file on purpose — see
│                                  parsers/registry.py's own docstring for why
│
├─ parsers/registry.py            parser_by_type() → {statement_type: ParserClass}
│                                  (each parser module self-registers via
│                                  register() at import time — see the
│                                  bottom of parsers/bofa.py, parsers/amex.py, etc.)
│
├─ parsers/base.py                StatementParser (shared base class),
│                                  ClientRegistry (reads clients/*.json),
│                                  vendor normalization, CC-payment classification
│
├─ parsers/<bank>.py              ParserClass(pdf_path)
│   │                               ├─ .parse()            extracts balances/
│   │                               │                       transactions from PDF text
│   │                               └─ .generate_report()  builds the printed report
│   │                                                       via parsers/report.py's
│   │                                                       shared section builders
│   │
│   └─ parsers/row_schema.py      TransactionRow — the shape a migrated parser's
│                                  parse() builds internally (see
│                                  REFACTORING_ROADMAP.md's Architecture Proposal)
│
├─ log_utils.py                   write_both_logs(), upsert_recon_log(),
│                                  entry_status(), get_client_notes(),
│                                  _assert_known_client / _assert_known_account_type
│                                  — reads/writes recon_log.json + reconciliation_log.csv
│
├─ drive_archiver.py              archive_statement() — uploads the PDF to
│                                  Google Drive, dedupes, keeps the 2 most recent
│
├─ sheets_updater.py              update_sheet() + append_recon_row() —
│                                  writes the Reconciliation Tracker Google
│                                  Sheet, only when the answer was "done"
│
└─ tools/github_clients.py        sync_up() — pushes the updated logs to the
                                   private Bookkeeping-clients repo
```

Called once per statement, in this order: detect type → look up the parser class in the registry → `parser.parse()` → (credit cards only) Vision fallback if the balance doesn't tie → `parser.generate_report()` → balance-check gate (halts immediately on a FAILED check) → `recon_log.json` written as `IN_PROGRESS` and pushed → report printed, QB confirmation asked → `reconciliation_log.csv` + `recon_log.json` written with the final status → Drive archive → (only if `done`) Google Sheet update.

---

## Key Features

- **Auto-detection** — identifies bank and account type from PDF text; no manual flags needed
- **Vision fallback** — if pdftotext produces numbers that don't tie, Claude Vision re-extracts the data automatically
- **Config-driven** — client behavior (vendor rules, payroll format, CC blocking) lives in `clients/*.json`; adding a client requires no code changes
- **Two-tier vendor normalization** — global rules for common vendors (Amazon, PG&E, etc.) with client-specific overrides; prompts to approve new descriptions
- **Client name normalization** — all name variants resolved to canonical form via the client registry before writing to logs
- **Google Drive archiving** — auto-archives reconciled PDFs by client/account type; deduplicates; keeps only the 2 most recent statements
- **MCP server** — Claude Desktop integration for running reconciliation from chat
- **Penny-perfect verification** — every report includes a balance check; a FAILED check halts the pipeline before logging
- **Append-only audit trail** — the Recon Log tab in Google Sheets is never overwritten, only appended to
- **CC blocking rules** — checking accounts are shown as blocked in the digest until their CC statements are reconciled

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `unknown` statement type | New bank format or corrupted PDF | Add a parser in `parsers/` |
| Balance check FAILED | Missing transactions or scanned PDF | Run with Vision enabled, or `--force` to bypass |
| Client not recognized | New client, no config yet | Script prompts you to create one interactively |
| Sheet not updated | `GITHUB_PAT_BOOKKEEPING` not set | Set env var or update manually via GitHub Actions |
| Wrong closing date warning | Statement date outside expected billing cycle | Verify the PDF is the right month |

For check payees OCR can't read (cursive, handwriting):
```bash
python3 reconcile_comprehensive.py statement.pdf --check-payee 1235='Jane Doe' --check-payee 1236='John Roe'
```
