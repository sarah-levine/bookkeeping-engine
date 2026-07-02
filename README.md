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

Every run passes through the same gate — how it responds to that gate depends on the mode you invoke it in:

```
Statement PDF ──Parse + Verify──▶ Gate ──Mode──▶ Outcome

                                    │
                ┌───────────────────┼───────────────────┐
                ▼                   ▼                   ▼
            ADVISORY            BLOCKING            ESCALATING
           (--dry-run)         (interactive)      (balance FAILED)
                │                   │                   │
                ▼                   ▼                   ▼
          Print report      "done / later"       Re-extract via
          only — no log,    prompt — waits        Claude Vision
          no Drive, no       for a human                │
          sheet update             │                    ▼
                                    ▼             Balance re-checked
                             ┌──────┴──────┐             │
                             ▼             ▼        still fails
                         done          later              │
                             │             │              ▼
                             ▼             ▼          Halt — do
                      Log DONE +    Log IN_PROGRESS   not log
                      archive to    skip sheet         bad data
                      Drive + sheet  for now
```

`--no-prompt` auto-answers `later` at the BLOCKING gate — for unattended/scripted runs. An unrecognized client or account type is its own hard stop (see `_assert_known_client` / `_assert_known_account_type`): it refuses to write in `--no-prompt` mode and asks for explicit confirmation interactively, rather than falling through any of the three gates above.

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
