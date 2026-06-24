# Reconciliation Skill

Run this skill when the user drops a bank or credit card statement PDF (or any file) and asks to reconcile it, or types `/reconcile`.

## Steps

1. **Run the reconciliation script** on the uploaded file:
   ```
   python3 reconcile_comprehensive.py <path-to-file>
   ```
   If the user hasn't specified the file path, ask for it.

2. **Read the output carefully.** Look for:
   - Detected client name and account type
   - Statement date and balances (beginning, ending)
   - Transaction count and any flagged issues
   - **Client notes** — if the script prints a "📋 Client notes:" section, read each bullet and relay the most important ones to the user before asking them to proceed

3. **Statement date check.** If the script warns that the statement date doesn't match the expected closing day, stop and confirm with the user before continuing. Do not write incorrect dates to the log.

4. **Ask the user to confirm QuickBooks reconciliation:**
   - Show the statement date and ending balance
   - Ask: "Have you reconciled this in QuickBooks?"
   - If yes, proceed to step 5
   - If no, pause — do not log until QuickBooks is reconciled

5. **Log the reconciliation** using `log_recon` from `tools/github_clients.py`:
   ```python
   from tools.github_clients import log_recon
   log_recon(
       client="<client_key>",
       client_name="<client_name>",
       account_type="<account_type>",
       statement_end_date="<YYYY-MM-DD>",
       beginning_balance="<amount>",
       ending_balance="<amount>",
       total_payments="<amount>",
       status="CLEAN",
   )
   ```
   Use `status="ISSUES"` if the script flagged unmatched transactions or balance mismatches.

6. **Confirm completion.** Tell the user which client and account type was logged, the statement date, and ending balance.

## Client Notes

Client-specific reminders are stored in `reconciliation_notes` inside each client's JSON config in Bookkeeping-clients. They print automatically during step 2. Common examples:
- De Anza checking: SBA EIDL loan entries, Marcone/Kaiser manual invoices
- De Anza credit cards: confirm payment check is in QuickBooks first
- De Anza payroll: run the payroll journal entry before reconciling

To add notes for a new client, add `"reconciliation_notes"` to their JSON config in Bookkeeping-clients. Keys can be an exact account_type, a category (`credit_cards`, `checking`, `savings`, `payroll`), or `"general"`.

## Error Handling

- **Unknown client:** Stop. Do not guess a client key. Ask the user to confirm the client name.
- **Unknown account type:** Stop. Ask the user to confirm before writing a new type to the log.
- **Balance mismatch:** Note the discrepancy. Ask the user to confirm before logging.
- **GITHUB_PAT_BOOKKEEPING not set:** The log step will fail. Tell the user they need to set this env var.
