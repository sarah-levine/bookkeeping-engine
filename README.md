## Script

**`reconcile_comprehensive.py`** - Master reconciliation script with auto-detection

The script automatically detects when a PDF is scanned (its text layer won't
produce numbers that tie to the penny) and falls back to Claude Vision to
re-extract the data. The same parser fields are populated either way, so
vendor normalization, aggregation, and report formatting all run through the
existing code path — there is no second pipeline to maintain.

To enable vision fallback for scanned statements:
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
pip install anthropic pymupdf --break-system-packages
```

Without the API key the script still runs, but if the parse doesn't tie out
it will warn loudly and produce a report from the partial data.

## Requirements

```bash
pip install PyMuPDF pytesseract --break-system-packages
apt-get install tesseract-ocr poppler-utils
```

## Usage

### Basic Usage
```bash
# Auto-detect statement type and reconcile
python3 reconcile_comprehensive.py statement.pdf output.txt
```

### With Check Payee Mapping
```bash
# For checks, specify payee names manually
python3 reconcile_comprehensive.py statement.pdf output.txt --check-payee 1235='Jane Doe'
```

### Examples
```bash
# Checking account
python3 reconcile_comprehensive.py Jan_2026_Checking.pdf Jan_Checking_Report.txt --check-payee 1235='Jane Doe'

# Credit card
python3 reconcile_comprehensive.py Feb_2026_CreditCard.pdf Feb_CC_Report.txt
```

## Features

### Auto-Detection
- Automatically detects Bank of America checking vs credit card statements
- No need to specify statement type

### Aggregation Rules (configurable per client)
- **Never aggregates**: ADP payroll transactions (each type kept separate for audit trail) or online transfers (each shown separately)
- **Always aggregates**: Square Inc, Amazon, and other vendors listed in the client's `vendor_rules` config

### Clean Output
- Removes clutter: Confirmation#, ID/DES/INDN fields
- Clean ADP descriptions (removes all ID codes)
- Extracts check payee names from images
- Mathematical verification to the penny

### Standard Monthly Reconciliation Format
- Statement Summary with beginning/ending balances
- Deposits and Credits section
- Withdrawals and Debits section
- Checks section (with payee names)
- Balance verification

## Supported Statement Types

- Bank of America Business Advantage Fundamentals Banking (Checking)
- Bank of America Business Advantage Cash Rewards (Credit Card)

## Output

Reports are saved in Standard Monthly Reconciliation format with:
- All transactions aggregated by vendor (where appropriate)
- Transaction counts shown for aggregated items
- Clean, readable vendor names
- Penny-perfect balance verification

## Troubleshooting

If statement type is not detected:
1. Check that the PDF is a Bank of America business statement
2. Verify the PDF is not corrupted
3. The script will show "unknown" and list supported types

For check payees:
- OCR often struggles with cursive handwriting
- Use `--check-payee` flag to manually specify payees
- Format: `--check-payee CHECK_NUM='Payee Name'`
