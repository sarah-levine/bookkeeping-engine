# Standard Monthly Reconciliation Workflow

## Overview
This document outlines the 15-step process for monthly bank statement reconciliation using the master reconciliation script.

## Prerequisites
- Python 3.6+
- `poppler-utils` (for pdftotext)
  - Ubuntu/Debian: `sudo apt-get install poppler-utils`
  - macOS: `brew install poppler`

## Supported Statement Types
- Chase Ink Business Credit Card
- Citi Checking Account
- Chase United Credit Card
- American Express Business Platinum
- Generic (auto-detection fallback)

## 15-Step Reconciliation Process

### 1. Download Statement PDFs
Download all monthly statements from your bank/card portals:
- Chase Ink Business
- Citi Checking
- Chase United
- AmEx Business Platinum
- Any other accounts

### 2. Organize Files
Create a working directory for the month:
```bash
mkdir ~/statements/2026-02
cd ~/statements/2026-02
```

### 3. Run Reconciliation Script
Process each statement:
```bash
python reconcile_statements.py chase_ink_feb2026.pdf > chase_ink_report.txt
python reconcile_statements.py citi_checking_feb2026.pdf > citi_report.txt
python reconcile_statements.py amex_business_feb2026.pdf > amex_report.txt
```

### 4. Review Auto-Detection
The script will automatically detect statement type. Verify it's correct:
```
Processing: chase_ink_feb2026.pdf
Detected type: Chase Ink Business Credit Card
```

### 5. Examine Transaction Count
Check that the number of transactions seems reasonable:
```
Processed 47 transactions
```

### 6. Review Vendor Aggregation
Look at the vendor breakdown in the report for any anomalies or duplicates

### 7. Verify Total Amounts
Cross-check the report totals against your statement closing balance

### 8. Identify Outliers
Review highest-spending vendors to ensure they're legitimate

### 9. Compare Month-over-Month
Compare current month's vendor totals against previous months for unusual changes

### 10. Categorize for QuickBooks
Use the vendor breakdown to prepare categorized entries for QuickBooks

### 11. Reconcile in QuickBooks
Enter transactions in QuickBooks and reconcile against statement ending balance

### 12. Document Discrepancies
Note any differences between parsed data and actual statements:
```bash
echo "Feb 2026 discrepancies:" >> reconciliation_notes.txt
```

### 13. Archive Reports
Save all generated reports:
```bash
mkdir -p ~/reconciliation_archive/2026/02
cp *_report.txt ~/reconciliation_archive/2026/02/
```

### 14. Update Tracking Spreadsheet
Log completion of reconciliation with:
- Statement period
- Total transactions
- Total amount
- Date reconciled
- Any notes

### 15. Backup Files
Backup both original PDFs and generated reports to cloud storage

## Script Usage

### Basic Usage
```bash
python reconcile_statements.py <statement.pdf>
```

### Save to File
```bash
python reconcile_statements.py <statement.pdf> <output.txt>
```

### Batch Processing
```bash
for pdf in *.pdf; do
    python reconcile_statements.py "$pdf" "${pdf%.pdf}_report.txt"
done
```

## Report Format

The script generates reports with:

1. **Header Section**
   - Statement type
   - File name
   - Generation timestamp

2. **Summary Statistics**
   - Total transactions
   - Total amount

3. **Vendor Breakdown**
   - Sorted by amount (descending)
   - Vendor name and total spent
   - Formatted with proper decimal precision

4. **Footer**
   - Grand total
   - Transaction count confirmation

## Technical Details

### Text Extraction
- Uses `pdftotext -layout` for preserving table structure
- Maintains column alignment for accurate parsing

### Transaction Parsing
- Regex patterns specific to each bank format
- Handles variations in date formats
- Normalizes whitespace in vendor names

### Vendor Aggregation
- Uses `defaultdict(Decimal)` for precise financial calculations
- Avoids floating-point arithmetic errors
- Aggregates multiple transactions per vendor

### Standard Output Format
```
================================================================================
RECONCILIATION REPORT - Chase Ink Business Credit Card
Statement: chase_ink_feb2026.pdf
Generated: 2026-02-11 14:30:00
================================================================================

Total Transactions: 47
Total Amount: $8,234.56

VENDOR BREAKDOWN:
--------------------------------------------------------------------------------
AMAZON WEB SERVICES                                           $    1,234.56
SQUARE *SALON EXAMPLE                                         $      892.30
COMCAST                                                       $      290.00
...
--------------------------------------------------------------------------------
TOTAL                                                         $    8,234.56
================================================================================
```

## Troubleshooting

### No Transactions Found
If the script reports "No transactions found":
1. The PDF may have a non-standard format
2. Check the first 1000 characters of extracted text (shown in error)
3. May need to create a custom parser for that statement type

### Incorrect Statement Type Detection
Override auto-detection by modifying the script to force a specific parser

### Missing Dependencies
```bash
# Install pdftotext
sudo apt-get install poppler-utils  # Ubuntu/Debian
brew install poppler                # macOS
```

## Customization

### Adding New Statement Types
1. Create a new parser class inheriting from `StatementParser`
2. Define the `statement_type` attribute
3. Implement the `parse()` method with appropriate regex
4. Add detection logic to `detect_statement_type()`

### Modifying Report Format
Edit the `generate_report()` method in `StatementParser` class

### Adjusting Regex Patterns
Update patterns in individual parser classes to match your specific statement formats

## Best Practices

1. **Always review auto-detection** - Verify the script detected the right statement type
2. **Spot-check transactions** - Manually verify a few high-value transactions
3. **Save original PDFs** - Keep source files for audit trail
4. **Use version control** - Track changes to parsing patterns over time
5. **Document edge cases** - Note any manual adjustments needed
6. **Regular updates** - Update regex patterns when banks change statement formats

## Monthly Checklist

- [ ] Download all statements
- [ ] Run reconciliation script on each
- [ ] Review vendor aggregations
- [ ] Cross-check totals
- [ ] Reconcile in QuickBooks
- [ ] Archive reports
- [ ] Update tracking spreadsheet
- [ ] Backup to cloud storage

## Support & Maintenance

When statement formats change:
1. Save a copy of the problematic PDF
2. Extract text manually: `pdftotext -layout statement.pdf -`
3. Identify the new pattern
4. Update the corresponding parser class
5. Test with multiple months to confirm

---

**Last Updated:** February 2026
**Version:** 1.0
