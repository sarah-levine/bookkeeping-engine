# Bookkeeping Skill — Full Instructions

> **Code changes are out of scope for this skill.** If a workflow surfaces a
> bug, a missing parser, a config field that needs updating, or any change to
> `.py`, `.json`, or `.md` files in the engine repo — stop, describe the issue
> clearly, and tell Sarah to take it to Claude Code. Do not edit files inline.
> The only writes this skill performs are to log files in Bookkeeping-clients
> via `sync_up()`.

> **Repo layout (read once):** code lives in the **public** `bookkeeping-engine`
> repo; client configs + logs live in the **private** `Bookkeeping-clients`
> repo. Scripts run from `/tmp/engine`; data, logs, and configs are pulled to
> `~/.bookkeeping/clients` via the REST API helper — no git auth URL needed.

## Mode Detection

Read the attached files and the user's message, then pick a mode:

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

If it's ambiguous, ask Sarah before proceeding.

### Checking account auto-sequence

When the statement is a **checking account** (amex_checking, bofa_checking,
bmo_checking, citi_checking, wells_fargo_checking, usbank_checking,
northern_trust_checking), always run all three steps automatically after Mode A
completes — no extra prompt needed:

**Step 1 — Mode A:** Reconcile as normal. Note the client key detected by the
script (printed in the first few lines of output).

**Step 2 — Mode C (payroll tie-out):** After reconciliation, grep
`payroll_log.csv` for any entries matching this client whose check date falls
within ±2 days of an ADP debit found on the statement. For each match, run the
tie-out and report the result inline.

```bash
grep "<client>" ~/.bookkeeping/clients/payroll_log.csv
```

If no payroll log entries exist for the period, note it and skip — don't ask
Sarah to re-upload payroll PDFs unless she asks.

**Step 3 — Mode E (CC payment tie-out):** After Mode C, look for CC payment
debits on the checking statement (keywords: AMEX EPAYMENT, CHASE CREDIT CRD,
AUTOPAY, AUTOPAYBUS, BOFA CREDIT). For each one found, grep
`reconciliation_log.csv` for a matching CC statement for the same client and
adjacent period, then run the tie-out.

```bash
grep "<client>" ~/.bookkeeping/clients/reconciliation_log.csv
```

If the CC statement hasn't been reconciled yet, note it as **PENDING** — don't
block or delay the payroll tie-out result.

**Format the combined output as three clearly labelled sections:**

```
════════════════════════════════════════════════════════
 RECONCILIATION — <Client> <Account> <Period>
════════════════════════════════════════════════════════
[Mode A report verbatim]

════════════════════════════════════════════════════════
 PAYROLL TIE-OUT
════════════════════════════════════════════════════════
[Mode C result for each payroll run found, or NONE FOUND if no log match]

════════════════════════════════════════════════════════
 CC PAYMENT TIE-OUT
════════════════════════════════════════════════════════
[Mode E result for each CC payment found, or PENDING if CC not yet reconciled]
```

---

## Shared Setup (all modes)

### Step 1 — Get the engine (public repo)

```bash
rm -rf /tmp/engine
git clone --depth 1 https://github.com/sarah-levine/bookkeeping-engine.git /tmp/engine
```

### Step 2 — Pull client configs + logs via REST API

The engine includes `tools/github_clients.py` which reads/writes the private
Bookkeeping-clients repo via the GitHub REST API — no git auth URL needed.

```bash
export BOOKKEEPING_CLIENTS_DIR=~/.bookkeeping/clients
export BOOKKEEPING_LOGS_DIR=~/.bookkeeping/clients
export BOOKKEEPING_NO_PROMPT=1

python3 -c "
import sys, os
sys.path.insert(0, '/tmp/engine')
os.environ['BOOKKEEPING_CLIENTS_DIR'] = os.path.expanduser('~/.bookkeeping/clients')
from tools.github_clients import sync_down
sync_down(include_configs=True)
print('Sync complete')
"
```

If `GITHUB_PAT_BOOKKEEPING` is unset, ask Sarah for the PAT and export it before
running the above — never write it to disk.

### Step 3 — Install dependencies

```bash
pip install --break-system-packages -q -r /tmp/engine/requirements.txt 2>/dev/null || \
  pip install --break-system-packages -q pdfplumber pypdf anthropic pymupdf 2>/dev/null || true
```

### Step 4 — Locate uploaded files

```bash
UPLOADS=$(ls -d /sessions/*/mnt/uploads 2>/dev/null | head -1)
OUTPUTS=$(ls -d /sessions/*/mnt/outputs 2>/dev/null | head -1)
mkdir -p "$OUTPUTS"
ls "$UPLOADS"
```

---

## Log Files (reference for all modes)

All logs live in `~/.bookkeeping/clients/` (pulled by Shared Setup) and are
written automatically by the scripts. `sync_up()` pushes them back to
Bookkeeping-clients and triggers Google Sheets sync — no manual git needed.

### `payroll_log.csv`
Columns: `client, client_name, check_date, bank_credit, balanced, run_timestamp`

### `reconciliation_log.csv`
Columns: `client, client_name, account_type, account_ending, statement_date, beginning_balance, ending_balance, total_payments, run_timestamp`

```bash
grep "<client>" ~/.bookkeeping/clients/reconciliation_log.csv
grep "<client>" ~/.bookkeeping/clients/payroll_log.csv
```

---

## Mode A — Monthly Reconciliation

### Run the reconciliation

```bash
python /tmp/engine/reconcile_comprehensive.py "$UPLOADS/<statement>.pdf" \
  "$OUTPUTS/<Client>_<Issuer>_Reconciliation_<closing-date>.txt"
```

The script auto-writes logs to `~/.bookkeeping/clients/` — no extra step needed.

### Handle output

Print the FULL report to Sarah verbatim inside a fenced code block — every
charge line, summary, payments, credits, exactly as emitted. Do **not**
summarize, reformat, regroup, or truncate.

Then call `present_files` on the saved `.txt` for download.

Watch for these signals in stderr:
- `⚠ Vision fallback unavailable: ANTHROPIC_API_KEY not set` → numbers unreliable on scanned PDFs. Ask Sarah to set `ANTHROPIC_API_KEY` and re-run.
- `⚠ pdftotext parse did not tie out — invoking Claude Vision fallback` → normal for scanned PDFs; should be followed by `✓ Vision fallback succeeded`.
- `⚠ Could not extract balance data` → switch to Mode G (Manual Statement Entry).

### Client Notes (check before QB confirmation)

After printing the report, scan the output for a `📋 Client notes:` section.
The script prints this automatically when the client's config has reminders for
that account type.

If a notes section appears:
1. Read each bullet to Sarah verbatim.
2. Ask her to confirm she has addressed each note before proceeding.
3. Only after confirmation, continue to QB Confirmation.

Client notes live in `reconciliation_notes` in each client's JSON config in
Bookkeeping-clients. To add or update notes, edit the client JSON — keys can
match an exact account_type, a category (`credit_cards`, `checking`, `savings`,
`payroll`), or `"general"` as a catch-all.

### Statement Date Warning

If the script prints a closing-day warning (e.g. `⚠ Statement date doesn't
match expected closing day`), stop and confirm with Sarah before continuing.
Do not push an incorrect date to the logs.

### QB Confirmation & Sync (REQUIRED after every statement)

After presenting the report and the downloadable file, **always** ask:

> "Done entering **[Client] [Account type] [Statement period]** in QuickBooks?"

Wait for Sarah to confirm (any affirmative — "yes", "done", "next", "✓", etc.)
before proceeding. Do not auto-advance.

Once Sarah confirms, sync back to Bookkeeping-clients and trigger Google Sheets:

```python
import sys, os
sys.path.insert(0, '/tmp/engine')
os.environ.setdefault('BOOKKEEPING_CLIENTS_DIR', os.path.expanduser('~/.bookkeeping/clients'))
from tools.github_clients import sync_up
sync_up("Reconciliation: <Client> <account_type> <statement_date>")
```

`sync_up()` pushes the log files AND fires the `logs-updated` dispatch which
automatically syncs the Google Sheets Reconciliation Tracker — no manual sheet
update needed.

**IN_PROGRESS entries:** If the script logged a status of `IN_PROGRESS` (e.g.,
balance didn't tie out but Sarah wants to continue), upgrade it to `CLEAN` once
resolved:

```bash
python3 /tmp/engine/mark_clean.py <client_key> <account_type> [<statement_date>]
```

Then call `sync_up()` again to push the updated status.

Only then present the next statement (if multiple were queued). Process one at a
time: present → notes → QB confirm → sync → next.

---

## Mode B — Payroll Journal Entry

### Identify the client

Read the client list from the registry — don't guess:

```bash
python3 -c "
import sys, os
sys.path.insert(0, '/tmp/engine')
os.environ['BOOKKEEPING_CLIENTS_DIR'] = os.path.expanduser('~/.bookkeeping/clients')
from parsers.base import _registry
for name, cfg in _registry._configs.items():
    if cfg.get('payroll_format'):
        print(cfg.get('payroll_key', '?'), '->', cfg.get('client_name', name), '->', cfg.get('payroll_format'))
"
```

### Run payroll.py

```bash
python /tmp/engine/payroll.py <client_key> "$UPLOADS/<payroll_details>.pdf"
```

Client-specific flags (check client config for `payroll_format`):
- `adp_payroll_departments` clients: may need a Payroll Liability PDF as a second argument
- `adp_payroll_tipped` clients: use `--pay-by-pay AMOUNT` if applicable
- `adp_labor_distribution` clients: script emits two separate journal entries — present them one at a time; wait for QB confirmation on the first before showing the second

After each run the script auto-upserts `payroll_log.csv`. Sync when Sarah confirms QB entry:

```python
import sys, os
sys.path.insert(0, '/tmp/engine')
os.environ.setdefault('BOOKKEEPING_CLIENTS_DIR', os.path.expanduser('~/.bookkeeping/clients'))
from tools.github_clients import sync_up
sync_up("Payroll: <Client> <check_date>")
```

### Display output

Print the full journal entry output verbatim in a fenced code block. If balance
status shows **OUT OF BALANCE**, report it prominently before Sarah uses the entry.

For clients with two journal entries (Labor Distribution format): present Agency
entry first, wait for QB confirmation, then present Admin entry.

---

## Mode C — Payroll + Checking Account Tie-Out

### Step 1: Get the expected bank credit

**If the ADP PDF is attached now** → run Mode B. The `bank_credit` is in the
journal entry output and logged automatically.

**If the payroll was run in a prior session** → read from the log:

```bash
grep "<client>" ~/.bookkeeping/clients/payroll_log.csv
```

Use the `bank_credit` column as the expected disbursement. Check `balanced` —
if FALSE, warn Sarah before proceeding.

### Step 2: Run reconcile_comprehensive.py on the checking statement (Mode A)

Follow Mode A steps. From the output, find ADP Wage Pay debits matching payroll:
amounts matching `bank_credit` (exactly or summed), descriptions containing:
ADP, WAGE PAY, PAYROLL, DIRECT DEPOSIT, FSDD, NET PAY, or the company name.

### Step 3: Cross-check and report

| Scenario | Result |
|---|---|
| One bank debit = bank_credit exactly | ✓ TIES OUT |
| Multiple debits summing to bank_credit | ✓ TIES OUT (show the breakdown) |
| Amounts don't match | ✗ DOES NOT TIE — show the variance |
| No payroll transaction found | ✗ NOT FOUND — check date range |
| balanced = FALSE in log | ⚠️ WARN — journal entry was out of balance when run |

**Report format:**

```
=== PAYROLL TIE-OUT: <Client> — <Check Date> ===

SOURCE: payroll_log.csv  [or: payroll.py run this session]
  Check Date:    MM/DD/YYYY
  Bank Credit:   $X,XXX.XX
  Balanced:      TRUE

BANK STATEMENT:
  MM/DD/YYYY   ADP WAGE PAY   $X,XXX.XX

RESULT: ✓ TIES OUT TO THE PENNY
```

**Edge cases:**
- Payroll may post 1–2 days after check date — widen the date window if no match.
- For Labor Distribution clients, check for two separate debits or a single combined debit.
- For multiple payroll runs in one period, check the log for each check date.

---

## Mode D — QA Verification Against QuickBooks

### Step 1: Build qb_data.json from screenshots

Read all QB Reconcile screenshots. Build this JSON:

```json
{
  "period": "MM/DD/YYYY",
  "beginning_balance": "0.00",
  "ending_balance": "0.00",
  "cleared_balance": "0.00",
  "difference": "0.00",
  "charges": [
    {"date": "MM/DD/YYYY", "vendor": "Vendor Name", "amount": "9.99", "checked": true}
  ],
  "payments_credits": [
    {"date": "MM/DD/YYYY", "vendor": "Vendor", "memo": "...", "amount": "0.00", "checked": true}
  ]
}
```

Only include items that are **checked** in QB. Use exact amounts and vendor names.
If items are scrolled off-screen, ask Sarah for more screenshots first.

Save to `~/.bookkeeping/clients/qb_data.json`.

### Step 2: Run qa_reconciliation.py

```bash
python /tmp/engine/qa_reconciliation.py "$UPLOADS/<statement>.pdf" \
  ~/.bookkeeping/clients/qb_data.json
```

### Step 3: Display results

Show the full markdown table output verbatim. Common issues:
- **❌ in QB, `—` in report**: In QB but not on the statement — possible duplicate or wrong period.
- **In report but not in QB**: Missing transaction to add in QuickBooks.
- **Amount mismatch**: Same vendor, different amounts — data entry error.

Do not delete `qb_data.json` — Sarah may iterate and re-run.

---

## Mode E — CC Payment Tie-Out (Checking → Credit Card)

### Step 1: Get the CC payment from the checking side

```bash
grep "<CLIENT>.*checking.*<MONTH>" ~/.bookkeeping/clients/reconciliation_log.csv
```

Find the CC payment debit in the reconciliation report (keywords: AMEX EPAYMENT,
CHASE CREDIT CRD, AUTOPAY, AUTOPAYBUS, BOFA CREDIT).

### Step 2: Get the CC payment from the CC side

```bash
grep "<CLIENT>.*amex\|chase_ink\|bofa_credit.*<MONTH>" ~/.bookkeeping/clients/reconciliation_log.csv
```

Use the `total_payments` column on the CC row.

### Step 3: Cross-check and report

| Scenario | Result |
|---|---|
| Checking debit = CC payment received exactly | ✓ TIES OUT |
| Amounts differ | ✗ DOES NOT TIE — show variance and both sources |
| CC statement not yet reconciled | Ask Sarah to run Mode A on the CC statement first |

**Report format:**

```
=== CC PAYMENT TIE-OUT: <Client> — <Period> ===

CHECKING STATEMENT (<account_ending>  <statement_date>):
  CC Payment debit:   $X,XXX.XX

CC STATEMENT (<account_ending>  <statement_date>):
  Payment received:   $X,XXX.XX

RESULT: ✓ TIES OUT TO THE PENNY
```

---

## Mode F — Add New Client

Follow the checklist in `ADDING_NEW_CLIENT.md` in the engine repo. Steps in order:

### Step 1: Create the client JSON in Bookkeeping-clients

Pull current configs first:
```python
import sys, os; sys.path.insert(0, '/tmp/engine')
os.environ['BOOKKEEPING_CLIENTS_DIR'] = os.path.expanduser('~/.bookkeeping/clients')
from tools.github_clients import sync_down
sync_down(include_configs=True)
```

Copy the example config and fill in all required fields:
```bash
cp /tmp/engine/clients/example_client.json ~/.bookkeeping/clients/<client_slug>.json
```

Minimum required fields:
- `client_name` — exact name as it appears on statements
- `canonical_name` — UPPER_SNAKE_CASE, used in all logs
- `aliases` — alternate names the parser might see
- `statement_types` — list of supported parser codes
- `vendor_rules` — normalization rules for common vendors

Ask Sarah for each value — do not guess. After filling in:

```python
from tools.github_clients import push_file
push_file('<client_slug>.json', message='Add <client_name> config')
```

**Verify:** The new client appears in the registry:
```bash
python3 -c "
import sys, os; sys.path.insert(0, '/tmp/engine')
os.environ['BOOKKEEPING_CLIENTS_DIR'] = os.path.expanduser('~/.bookkeeping/clients')
from parsers.base import _registry
print([k for k in _registry._configs.keys()])
"
```

### Step 2: Add cell map entries to sheets_config.json

Pull and edit `~/.bookkeeping/clients/sheets_config.json`. Add one entry to
`cell_map` per account type, keyed as `CANONICAL_NAME|account_type`. Ask Sarah
for the Google Sheet row — look at the Reconciliation Tracker to find it.

Also add to `client_names`:
```json
"client_names": { "MY_CLIENT": "My Client Display Name" }
```

Push when done:
```python
from tools.github_clients import push_file
push_file('sheets_config.json', message='Add <client> to sheets_config')
```

### Step 3: Add client_key_map alias if needed

If the parser detects the client under a different name than the tracker key,
add both forms to `client_key_map` in `sheets_config.json`.

### Step 4: Add acct_type_map entries if needed

If any account type key from reconciliation doesn't match the cell_map key
exactly, add normalization to `acct_type_map` in `sheets_config.json`.

### Step 5: Add the client to digest_config.json

Pull and edit `~/.bookkeeping/clients/digest_config.json`:

**a.** `client_display_names` — add lowercased raw name variants:
```json
"my client llc": "My Client",
"my_client_llc": "My Client"
```

**b.** `tracker` array — add in display order with label, key, fallback_date per account.

**c.** `cc_blocking_rules` — add if the client has a CC that must be reconciled
before checking/savings.

Push when done:
```python
from tools.github_clients import push_file
push_file('digest_config.json', message='Add <client> to digest_config')
```

### Step 6: Test reconciliation

```bash
python /tmp/engine/reconcile_comprehensive.py "$UPLOADS/<test_statement>.pdf"
```

Check: correct client detected, no "unknown client" warning, logs update with
correct `client` and `account_type` values.

### Step 7: Verify the tracker sheet

```bash
python3 -c "
import sys, os; sys.path.insert(0, '/tmp/engine')
os.environ['BOOKKEEPING_CLIENTS_DIR'] = os.path.expanduser('~/.bookkeeping/clients')
from sheets_updater import update_sheet
update_sheet('MY_CLIENT', 'account_type', 'MM/DD/YY')
"
```

If it says "No sheet cell mapped", recheck the `cell_map` key format in step 2.

### Step 8: Verify the morning digest

```bash
python /tmp/engine/send_morning_digest.py --date YYYY-MM-DD
```

Confirm the client appears with the correct display name. If it shows the raw
key, add the lowercase variant to `client_display_names`.

### Step 9: Normalize logs

```bash
python3 /tmp/engine/repair_logs.py
```

Then push final state:
```python
from tools.github_clients import sync_up
sync_up("Onboard <client_name>: configs, cell map, tracker entry")
```

---

## Mode G — Manual Statement Entry

Use when a statement PDF is scanned or unparseable (script says
`⚠ Could not extract balance data` or Vision fallback unavailable).

### Step 1: Pull manual_statements.json

```python
import sys, os; sys.path.insert(0, '/tmp/engine')
os.environ['BOOKKEEPING_CLIENTS_DIR'] = os.path.expanduser('~/.bookkeeping/clients')
from tools.github_clients import pull_file
pull_file('manual_statements.json')
```

### Step 2: Add an entry for the statement

Edit `~/.bookkeeping/clients/manual_statements.json`. Ask Sarah for all values:

```json
{
  "active": "<month_key>",
  "<month_key>": {
    "statement_type": "bmo_checking",
    "client_name": "<client_name>",
    "period": "MM/DD/YYYY–MM/DD/YYYY",
    "previous_balance": "0.00",
    "new_balance": "0.00",
    "credits": [
      {"date": "MM/DD/YYYY", "description": "Vendor Name", "amount": "0.00"}
    ],
    "checks": [],
    "debits": [
      {"date": "MM/DD/YYYY", "description": "Vendor Name", "amount": "0.00"}
    ]
  }
}
```

Push the updated file:
```python
from tools.github_clients import push_file
push_file('manual_statements.json', message='Manual entry: <client> <period>')
```

### Step 3: Run manual_statement_entry.py

```bash
python /tmp/engine/manual_statement_entry.py \
  "$OUTPUTS/<Client>_Manual_<period>.txt" --month <month_key>
```

### Step 4: Handle output and sync

Follow the same output, client notes, and QB confirmation flow as Mode A.
After confirmation:

```python
from tools.github_clients import sync_up
sync_up("Manual reconciliation: <Client> <account_type> <statement_date>")
```

---

## Error Handling

- **ImportError / ModuleNotFoundError**: `pip install <module> --break-system-packages` and retry.
- **Vision fallback unavailable**: Ask Sarah to set `ANTHROPIC_API_KEY`; switch to Mode G if needed.
- **Unknown client detected**: Stop. Ask Sarah to confirm the client name — do not guess or create a new key without her confirmation.
- **Unknown account type**: Stop. Ask Sarah before writing a new type to the log.
- **Statement date warning**: Stop. Confirm the correct date with Sarah before syncing.
- **GITHUB_PAT_BOOKKEEPING not set**: Ask Sarah for the PAT — export it for this session only, never write to disk.
- **sync_up() fails / push rejected**: The PAT may have expired. Ask Sarah for a new one.
- **`repair_logs.py` needed**: Run after any manual log edits to normalize client key variants and deduplicate rows.
