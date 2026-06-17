# Quick Reference Guide - Bank Statement Reconciliation

## Most Common Commands

### Process a Single Statement
```bash
python reconcile_statements.py statement.pdf
```

### Save Report to File
```bash
python reconcile_statements.py statement.pdf report.txt
```

### Process All PDFs in Current Directory
```bash
for pdf in *.pdf; do
    python reconcile_statements.py "$pdf" "${pdf%.pdf}_report.txt"
done
```

### View Report Without Saving
```bash
python reconcile_statements.py statement.pdf | less
```

## Typical Monthly Workflow

```bash
# 1. Create month directory
mkdir ~/statements/2026-02
cd ~/statements/2026-02

# 2. Download statements (manual step via browser)

# 3. Process all statements
python reconcile_statements.py chase_ink_feb2026.pdf > chase_ink.txt
python reconcile_statements.py citi_checking_feb2026.pdf > citi.txt
python reconcile_statements.py amex_business_feb2026.pdf > amex.txt

# 4. Quick review of all totals
grep "TOTAL" *.txt

# 5. Archive when done
mkdir -p ~/reconciliation_archive/2026/02
cp *.txt ~/reconciliation_archive/2026/02/
```

## Account-Specific Examples

### Chase Ink Business
```bash
python reconcile_statements.py ~/Downloads/chase_ink_statement.pdf > chase_ink_report.txt
```

### Citi Checking
```bash
python reconcile_statements.py ~/Downloads/citi_checking.pdf > citi_report.txt
```

### AmEx Business Platinum
```bash
python reconcile_statements.py ~/Downloads/amex_business.pdf > amex_report.txt
```

### Chase United
```bash
python reconcile_statements.py ~/Downloads/chase_united.pdf > united_report.txt
```

## Helpful Tips

### Check if pdftotext is installed
```bash
which pdftotext
```

### Install pdftotext if missing
```bash
# macOS
brew install poppler

# Ubuntu/Debian
sudo apt-get install poppler-utils
```

### Test PDF extraction manually
```bash
pdftotext -layout statement.pdf -
```

### Find all PDF statements
```bash
find ~/Downloads -name "*statement*.pdf" -mtime -60
```

### Compare totals across months
```bash
grep "Total Amount" ~/reconciliation_archive/2026/*/chase_ink.txt
```

## Troubleshooting One-Liners

### See what the script detected
```bash
python reconcile_statements.py statement.pdf 2>&1 | grep "Detected type"
```

### Count transactions found
```bash
python reconcile_statements.py statement.pdf 2>&1 | grep "Processed"
```

### Extract just vendor totals
```bash
python reconcile_statements.py statement.pdf | grep -A 100 "VENDOR BREAKDOWN" | grep "^\w"
```

## Integration with QuickBooks

After generating reports, you can:

1. Open report in text editor
2. Copy vendor breakdown section
3. Use for categorizing transactions in QuickBooks
4. Cross-reference totals during reconciliation

## Automation Ideas

### Weekly check for new statements
```bash
# Add to cron:
0 9 * * 1 python /path/to/reconcile_statements.py ~/Downloads/*statement*.pdf
```

### Email reports automatically
```bash
python reconcile_statements.py statement.pdf report.txt && \
  mail -s "Monthly Reconciliation" you@email.com < report.txt
```

## File Organization

Recommended structure:
```
~/statements/
  ├── 2026-01/
  │   ├── chase_ink.pdf
  │   ├── chase_ink_report.txt
  │   ├── citi_checking.pdf
  │   └── citi_checking_report.txt
  ├── 2026-02/
  │   └── (current month)
  └── reconcile_statements.py (symlink)
```

## Quick Checks

### Verify all accounts processed
```bash
ls -1 *.txt | wc -l
# Should match number of accounts
```

### Sum all statement totals
```bash
grep "^TOTAL" *.txt | awk '{sum+=$NF} END {print sum}'
```

### Find largest vendors across all accounts
```bash
cat *_report.txt | grep -E '^\w' | sort -k2 -rn | head -20
```
