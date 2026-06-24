# Adding a New Client — Full Checklist

Steps learned from onboarding Delta Dental Inc. Follow in order; each step has a
verification so you know it worked before moving on.

---

## 1. Create the client JSON in `Bookkeeping-clients`

```bash
cd /tmp/Bookkeeping-clients
cp example_client.json your_client.json   # rename to e.g. delta_dental.json
```

Fill in the required fields (see **Field Reference** below). At minimum:

- `client_name` — exact name as it appears on statements
- `canonical_name` — UPPER_SNAKE_CASE key used everywhere in logs
- `statement_types` — which parsers apply
- `aliases` — any alternate names the parser might see

Commit and push to `Bookkeeping-clients`:

```bash
git add your_client.json && git commit -m "Add <client> config" && git push
```

**Verify:** `python3 -c "from log_utils import load_client_registry; r = load_client_registry(); print([c.canonical_name for c in r.clients])"` — your new client should appear.

---

## 2. Add cell map entries to `sheets_config.json`

Open `/tmp/Bookkeeping-clients/sheets_config.json`. Add one entry to `cell_map`
for each account type the client has, plus payroll:

```json
"cell_map": {
  "MY_CLIENT_LLC|bofa_checking":  "B5",
  "MY_CLIENT_LLC|bofa_credit":    "C5",
  "MY_CLIENT_LLC|payroll":        "D5"
}
```

The key format is `CANONICAL_NAME|account_type`. Find the right cells by
looking at the Reconciliation Tracker spreadsheet — each client has a row.

Also add the client to `client_names` (human-readable label for the sheet
sync log):

```json
"client_names": {
  "MY_CLIENT_LLC": "My Client LLC"
}
```

---

## 3. Add `client_key_map` alias if needed

If the client's `canonical_name` differs from what the reconciliation log
writes (e.g. the parser detects `"DELTA DENTAL INC"` but the tracker key is
`DELTA_DENTAL_INC`), add an alias in `sheets_config.json`:

```json
"client_key_map": {
  "DELTA DENTAL INC": "DELTA_DENTAL_INC",
  "DELTA_DENTAL_INC": "DELTA_DENTAL_INC"
}
```

Include both the raw and normalized forms to be safe.

---

## 4. Add `acct_type_map` entries for non-standard account types

If any account type key from the reconciliation doesn't match the `cell_map`
key exactly (e.g. `chase_sapphire_preferred` vs `chase_sapphire`), add a
normalization in `sheets_config.json`:

```json
"acct_type_map": {
  "chase_sapphire_preferred": "chase_sapphire",
  "chase_sapphire_reserve":   "chase_sapphire"
}
```

---

## 5. Add the client to `digest_config.json`

Open `/tmp/Bookkeeping-clients/digest_config.json`.

**a. `client_display_names`** — add all raw name variants (lowercased) that
appear in the logs:

```json
"client_display_names": {
  "my client llc": "My Client",
  "my_client_llc": "My Client"
}
```

**b. `tracker`** — add an entry to the array in display order:

```json
{
  "client": "My Client",
  "client_keys": ["MY_CLIENT_LLC"],
  "accounts": [
    {"label": "BofA Checking",    "key": "bofa_checking",  "fallback_date": "MM/DD/YY"},
    {"label": "BofA Credit Card", "key": "bofa_credit",    "fallback_date": "MM/DD/YY"},
    {"label": "Payroll",          "key": "payroll",        "fallback_date": "MM/DD/YY"}
  ]
}
```

Set `fallback_date` to the most recently reconciled date for each account —
this appears in the tracker until the live log has a newer date.

**c. `cc_blocking_rules`** (if the client has a CC that must be reconciled
before checking/savings):

```json
"My Client": {
  "blocked": ["bofa_checking", "bofa_savings"],
  "payroll_blocked": ["payroll"],
  "checking_key": "bofa_checking",
  "cc_blockers": [
    {"key": "bofa_credit", "closing_day": 15}
  ]
}
```

Commit and push `sheets_config.json` and `digest_config.json` to
`Bookkeeping-clients`.

---

## 6. Run a test reconciliation with `--no-prompt`

```bash
python /tmp/engine/reconcile_comprehensive.py <statement.pdf>
```

Check that:
- The correct client is detected (no "unknown client" warning)
- Transactions parse without errors
- `recon_log.json` gets a new entry with the right `client` and `account_type`
- `reconciliation_log.csv` is updated with the right key

---

## 7. Verify the tracker sheet updates

```bash
python3 -c "
from sheets_updater import update_sheet
update_sheet('MY_CLIENT_LLC', 'bofa_checking', 'MM/DD/YY')
"
```

Open the spreadsheet and confirm the cell updated. If it says "No sheet cell
mapped", recheck step 2 — the `cell_map` key format or the cell reference.

---

## 8. Verify the morning digest

```bash
python3 send_morning_digest.py --date YYYY-MM-DD
```

Check that the client appears in the tracker grid with the correct dates and
that recon run cards display the friendly name (not the raw canonical key).

If the name shows as the raw key, add the lowercase variant to
`client_display_names` in `digest_config.json` (step 5a).

---

## 9. Run `repair_logs.py`

```bash
python3 repair_logs.py
```

This normalizes any client key variants already in the log, deduplicates rows,
and mirrors missing payroll entries. Safe to run any time — it's idempotent.

---

## Config File Structure

```json
{
  "client_name": "My Client LLC",
  "aliases": ["My Client", "MC LLC"],
  "canonical_name": "MY_CLIENT_LLC",
  "bank_account": "1000 · Checking",
  "statement_types": ["bofa_checking", "bofa_credit"],
  "cardholders": [],
  "payroll_vendors": ["ADP WAGE PAY", "ADP TAX", "ADP PAY-BY-PAY"],
  "cc_keywords": ["BANK OF AMERICA CREDIT CARD"],
  "vendor_rules": [
    { "contains": "COMCAST",   "normalize_to": "Comcast/Xfinity" },
    { "contains": "AMAZON",    "normalize_to": "Amazon" },
    { "contains": "PGANDE",    "normalize_to": "PG&E" }
  ]
}
```

## Field Reference

| Field | Required | Description |
|---|---|---|
| `client_name` | ✓ | Exact name as it appears on statements |
| `aliases` | | Other names the client appears as (e.g. cardholder names) |
| `canonical_name` | ✓ | UPPER_SNAKE_CASE key used in logs and cell map |
| `statement_types` | | Which parsers apply (see list below) |
| `cardholders` | | For multi-cardholder AmEx — list of cardholder names |
| `payroll_vendors` | | Keywords that route transactions to the Payroll section |
| `cc_keywords` | | Keywords that identify credit card payment transactions |
| `vendor_rules` | | List of normalization rules (see below) |

## Vendor Rule Options

```json
{ "contains": "KEYWORD",                               "normalize_to": "Clean Name" }
{ "contains": "KEYWORD", "also_contains": "KEYWORD2",  "normalize_to": "Clean Name" }
{ "contains": "KEYWORD", "also_contains_any": ["A","B"],"normalize_to": "Clean Name" }
{ "starts_with": "KEYWORD",                            "normalize_to": "Clean Name" }
```

Rules are evaluated in order — first match wins.

## Supported Statement Types

| Code | Bank | Account |
|---|---|---|
| `bofa_checking` | Bank of America | Business Checking |
| `bofa_credit` | Bank of America | Business Credit Card |
| `bofa_savings` | Bank of America | Business Savings |
| `citi_checking` | Citi | Business Checking |
| `citi_savings` | Citi | Business Savings |
| `citi_visa_costco` | Citi | Costco Anywhere Visa |
| `chase_ink` | Chase | Ink Business Credit Card |
| `chase_united` | Chase | United Credit Card |
| `chase_sapphire` | Chase | Sapphire Preferred / Reserve |
| `amex` | American Express | Business Credit Card |
| `amex_checking` | American Express | Business Checking |
| `bmo_checking` | BMO | Business Checking |
| `usbank_checking` | US Bank | Business Checking |
| `wells_fargo_checking` | Wells Fargo | Initiate Business Checking |
| `wells_fargo_credit` | Wells Fargo | Signify Business Essential Credit Card |
| `northern_trust_checking` | Northern Trust | Basic Business Checking |

If your client uses a bank not listed here, a new parser class needs to be
added to `parsers/` — but that's the only time code changes are required.
