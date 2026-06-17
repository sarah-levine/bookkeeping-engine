# Bank Statement Reconciliation Tool

Automated bank statement reconciliation tool that extracts, parses, and aggregates transactions from PDF statements.

## Features

- 🔍 **Auto-detects** statement types (Chase, Citi, AmEx, and more)
- 📊 **Aggregates** transactions by vendor using precise decimal arithmetic
- 📄 **Generates** standardized reconciliation reports
- 🔧 **Extensible** - easily add new statement formats
- 💼 **Professional** - follows financial best practices with Decimal precision

## Supported Statement Types

- Chase Ink Business Credit Card
- Citi Checking Account
- Chase United Credit Card
- American Express Business Platinum
- Generic (auto-detection fallback)

## Quick Start

### Prerequisites

```bash
# macOS
brew install poppler

# Ubuntu/Debian
sudo apt-get install poppler-utils

# Python 3.6+
python --version
```

### Basic Usage

```bash
# Process a single statement
python reconcile_statements.py statement.pdf

# Save report to file
python reconcile_statements.py statement.pdf report.txt

# Process all PDFs in directory
for pdf in *.pdf; do
    python reconcile_statements.py "$pdf" "${pdf%.pdf}_report.txt"
done
```

## Example Output

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
PG&E                                                          $      441.59
...
--------------------------------------------------------------------------------
TOTAL                                                         $    8,234.56
================================================================================
```

## Documentation

- [RECONCILIATION_WORKFLOW.md](RECONCILIATION_WORKFLOW.md) - Complete 15-step monthly reconciliation process
- [QUICK_REFERENCE.md](QUICK_REFERENCE.md) - Command cheatsheet and common usage patterns

## How It Works

1. **Text Extraction**: Uses `pdftotext -layout` to preserve table structure
2. **Pattern Matching**: Regex patterns detect transactions based on statement type
3. **Vendor Aggregation**: `defaultdict(Decimal)` ensures precise financial calculations
4. **Report Generation**: Standardized output format for easy reconciliation

## Technical Details

### Architecture

```
StatementParser (base class)
    ├── ChaseInkParser
    ├── CitiCheckingParser
    ├── ChaseUnitedParser
    ├── AmexBusinessParser
    └── GenericParser
```

### Key Technologies

- **pdftotext**: PDF text extraction with layout preservation
- **Python regex**: Transaction pattern matching
- **Decimal**: Precise financial arithmetic (no floating-point errors)
- **defaultdict**: Automatic vendor aggregation

## Adding New Statement Types

```python
class NewBankParser(StatementParser):
    statement_type = "New Bank Credit Card"
    
    def parse(self):
        pattern = r'(\d{2}/\d{2})\s+(.+?)\s+(\d+\.\d{2})$'
        
        for line in self.text.split('\n'):
            match = re.search(pattern, line)
            if match:
                date, description, amount = match.groups()
                self.transactions.append({
                    'date': date,
                    'vendor': description.strip(),
                    'amount': amount
                })
```

Then add detection logic in `detect_statement_type()`.

## Use Cases

- **Monthly Reconciliation**: Automate statement review process
- **Expense Tracking**: Aggregate spending by vendor across accounts
- **QuickBooks Integration**: Generate categorized transaction lists
- **Financial Analysis**: Compare month-over-month spending patterns
- **Audit Trail**: Maintain consistent reconciliation documentation

## Workflow Integration

### Monthly Reconciliation Process

1. Download statements from bank portals
2. Run reconciliation script on each PDF
3. Review vendor aggregations for anomalies
4. Import into QuickBooks or accounting software
5. Archive reports for record-keeping

### File Organization

```
~/statements/
  ├── 2026-01/
  │   ├── chase_ink.pdf
  │   ├── chase_ink_report.txt
  │   ├── citi_checking.pdf
  │   └── citi_checking_report.txt
  └── 2026-02/
      └── (current month)
```

## Troubleshooting

### No transactions found?

The PDF format may not match expected patterns. Check extracted text:

```bash
pdftotext -layout statement.pdf -
```

### Wrong statement type detected?

The auto-detection can be overridden by modifying the parser selection in the script.

### pdftotext not found?

Install poppler-utils:
- macOS: `brew install poppler`
- Ubuntu/Debian: `sudo apt-get install poppler-utils`

## Best Practices

✅ Always verify auto-detection is correct  
✅ Spot-check high-value transactions manually  
✅ Keep original PDFs for audit trail  
✅ Archive reports monthly  
✅ Update regex patterns when bank formats change  

## Privacy & Security

⚠️ **Important**: This tool processes sensitive financial data locally. 

- Never commit actual statement PDFs to version control
- Add `*.pdf` and `*_report.txt` to `.gitignore`
- Store statements securely with appropriate access controls
- Consider encrypting archived statements

## Contributing

To add support for new statement types:

1. Create a new parser class
2. Define appropriate regex patterns
3. Add detection logic
4. Test with multiple months of statements
5. Submit a pull request

## License

MIT License - See LICENSE file for details

## Acknowledgments

Built to streamline monthly financial reconciliation workflows for small business owners and financial professionals.

---

**Maintained by**: Sarah  
**Last Updated**: February 2026  
**Version**: 1.0
