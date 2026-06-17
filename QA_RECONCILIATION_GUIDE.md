# QA Reconciliation - QuickBooks vs Statement Comparison

After running the standard monthly reconciliation, use `qa_reconciliation.py`
to verify that QuickBooks matches the statement line-by-line.

## When to Use

After Sarah enters transactions into QuickBooks and is in the
**Reconcile Credit Card / Bank Account** screen, take screenshots of:
1. Charges & Cash Advances side (scroll through all items)
2. Payments and Credits side
3. Bottom summary showing Beginning Balance, totals, Ending Balance, Difference

## Input Format

Create a `qb_data.json` file with QB checked items:

```json
{
  "period": "03/29/2026",
  "beginning_balance": "13044.43",
  "ending_balance": "32265.76",
  "cleared_balance": "31252.69",
  "difference": "-1013.07",
  "charges": [
    {"date": "02/27/2026", "vendor": "Uber", "amount": "9.99", "checked": true},
    {"date": "02/27/2026", "vendor": "MLS Next NY", "amount": "60.00", "checked": true}
  ],
  "payments_credits": [
    {"date": "03/12/2026", "vendor": "Hilton", "memo": "CC CRED",
     "amount": "1144.98", "checked": true},
    {"date": "03/13/2026", "vendor": "AutoPay Payment", "memo": "TRANSFER",
     "amount": "13044.43", "checked": true}
  ]
}
```

**Important**: Include only items that are **CHECKED** in QB (will be marked
as cleared). Unchecked items should not be in the JSON.

## Running the Script

```bash
cd /home/claude/Bookkeeping
python qa_reconciliation.py /mnt/user-data/uploads/2026-03-29.pdf qb_data.json
```

## Output

Produces three markdown tables:

1. **CHARGES & CASH ADVANCES** - vendor-by-vendor with QB amount, Report amount, MATCH?
2. **PAYMENTS & CREDITS** - same format
3. **SUMMARY** - balance comparison + listed issues

Items present in only QB or only the Report are flagged with ❌.

## Example Issues Detected

- **Missing from QB**: Vendor appears in report but not in QB checked items
- **Missing from Report**: Vendor checked in QB but not on statement (likely
  a duplicate or wrong period item)
- **Amount mismatch**: Same vendor, different amounts (data entry error)
- **Balance off**: QB cleared balance doesn't match statement ending balance

## Workflow

1. Run `reconcile_comprehensive.py` to produce the reconciliation report
2. Sarah enters transactions into QB
3. Take screenshots of QB Reconcile screen
4. Read screenshots → build `qb_data.json`
5. Run `qa_reconciliation.py` to verify match
6. Fix any issues in QB
7. Re-screenshot and re-run until everything matches ✅
