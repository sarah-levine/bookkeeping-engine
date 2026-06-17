#!/usr/bin/env python3
"""
qa_reconciliation.py — Vendor-by-vendor QA comparison of QuickBooks reconciliation
                       against the bank statement reconciliation output.

Produces a clean markdown table:
| DATE | VENDOR | QB AMOUNT | REPORT AMOUNT | MATCH? |

Usage:
    # Step 1: Claude reads the QB reconcile screen and produces qb_data.json
    # Step 2: Run this script:
    python qa_reconciliation.py <bank_statement.pdf> <qb_data.json>

    # Or pass QB JSON inline:
    python qa_reconciliation.py <bank_statement.pdf> --json '<json_string>'

QB JSON FORMAT:
{
  "period": "03/29/2026",
  "beginning_balance": "13044.43",
  "ending_balance": "32265.76",
  "cleared_balance": "31252.69",
  "difference": "-1013.07",
  "charges": [
    {"date": "02/27/2026", "vendor": "Uber", "amount": "9.99", "checked": true},
    ...
  ],
  "payments_credits": [
    {"date": "03/12/2026", "vendor": "Hilton", "memo": "CC CRED",
     "amount": "1144.98", "checked": true},
    ...
  ]
}

Output: Markdown tables (Charges & Payments/Credits) plus a summary.
"""

import sys
import os
import json
import subprocess
import re
from decimal import Decimal
from difflib import SequenceMatcher


# ── helpers ──────────────────────────────────────────────────────────────────

def d(val):
    """Convert to Decimal safely."""
    if isinstance(val, Decimal):
        return val
    s = str(val).replace(',', '').replace('$', '').strip() or '0'
    try:
        return Decimal(s)
    except Exception:
        return Decimal('0')


def normalize_vendor(name):
    """Normalize vendor name for fuzzy matching."""
    if not name:
        return ''
    n = name.upper().strip()
    # Strip common qualifiers and parenthetical counts
    n = re.sub(r'\s*\(\d+\s*(txns?|stays?)?\s*\)', '', n)
    n = re.sub(r'[^A-Z0-9 ]', '', n)
    n = re.sub(r'\s+', ' ', n).strip()
    return n


def vendor_similarity(a, b):
    """0..1 similarity score between two vendor names."""
    na, nb = normalize_vendor(a), normalize_vendor(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.9
    return SequenceMatcher(None, na, nb).ratio()


# ── reconciliation runner ────────────────────────────────────────────────────

def run_reconciliation(pdf_path):
    # If a .txt file is passed instead of a PDF, treat it as a pre-generated report
    if pdf_path.endswith('.txt'):
        with open(pdf_path) as f:
            return f.read()
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'reconcile_comprehensive.py')
    result = subprocess.run(['python', script, pdf_path, '--force'],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Reconciliation script failed:\n{result.stderr}")
    return result.stdout


def parse_recon_output(recon_text):
    """Parse the reconcile_comprehensive.py output into structured data."""
    data = {
        'beginning_balance': None,
        'ending_balance': None,
        'charges': [],
        'credits': [],
        'payments': [],
    }

    for line in recon_text.splitlines():
        m = re.search(r'Previous Balance[:\s]+\$\s*([\d,]+\.\d{2})', line)
        if m and data['beginning_balance'] is None:
            data['beginning_balance'] = Decimal(m.group(1).replace(',', ''))
        m = re.search(r'New Balance[:\s]+\$\s*([\d,]+\.\d{2})', line)
        if m and data['ending_balance'] is None:
            data['ending_balance'] = Decimal(m.group(1).replace(',', ''))

    # Split by section
    sections = re.split(r'={40,}', recon_text)
    current_section = None
    for chunk in sections:
        chunk_upper = chunk.upper()
        if 'CHARGES' in chunk_upper and 'CASH ADVANCE' not in chunk_upper:
            current_section = 'charges'
        elif 'CREDITS' in chunk_upper or 'RETURNS' in chunk_upper:
            current_section = 'credits'
        elif 'PAYMENTS' in chunk_upper:
            current_section = 'payments'
        else:
            current_section = None

        if not current_section:
            continue

        for line in chunk.splitlines():
            m = re.match(
                r'^\s*(\d{1,2}/\d{1,2}/\d{2,4})\s+(.+?)\s+\$\s*([\d,]+\.\d{2})\s*$',
                line.strip()
            )
            if m:
                data[current_section].append({
                    'date': m.group(1),
                    'vendor': m.group(2).strip(),
                    'amount': Decimal(m.group(3).replace(',', '')),
                    'matched': False,
                })
    return data


# ── matching logic ────────────────────────────────────────────────────────────

def find_match(qb_item, report_items, threshold=0.5):
    """Find best match for qb_item in report_items list. Returns index or None."""
    qb_amt = d(qb_item.get('amount', 0))
    qb_vendor = qb_item.get('vendor', '')
    qb_date = qb_item.get('date', '')

    best_idx = None
    best_score = 0.0

    for idx, ri in enumerate(report_items):
        if ri.get('matched'):
            continue
        # Amount must match within 1 cent
        if abs(d(ri['amount']) - qb_amt) > Decimal('0.01'):
            continue
        # Score on vendor similarity
        sim = vendor_similarity(qb_vendor, ri['vendor'])
        # Boost if dates match
        if qb_date and ri.get('date'):
            qb_d = qb_date[-5:].replace('/', '')  # MM/DD-ish
            ri_d = ri['date'][-5:].replace('/', '')
            if qb_d == ri_d:
                sim += 0.2
        if sim > best_score:
            best_score = sim
            best_idx = idx

    if best_idx is not None and best_score >= threshold:
        return best_idx
    return None


# ── table building ────────────────────────────────────────────────────────────

def build_section_table(section_name, qb_items, report_items):
    """Build markdown table for one section. Modifies report_items in place
    (sets 'matched'=True on matched rows)."""

    rows = []  # list of tuples (date, vendor, qb_amt, report_amt, match)

    # First pass: match each QB item to a report item
    for qb in qb_items:
        idx = find_match(qb, report_items)
        if idx is not None:
            ri = report_items[idx]
            ri['matched'] = True
            rows.append({
                'date': qb.get('date', ''),
                'vendor': qb.get('vendor', ''),
                'qb_amount': d(qb.get('amount', 0)),
                'report_amount': d(ri['amount']),
                'match': True,
            })
        else:
            rows.append({
                'date': qb.get('date', ''),
                'vendor': qb.get('vendor', ''),
                'qb_amount': d(qb.get('amount', 0)),
                'report_amount': None,
                'match': False,
            })

    # Second pass: add unmatched report items
    for ri in report_items:
        if not ri.get('matched'):
            rows.append({
                'date': ri.get('date', ''),
                'vendor': ri.get('vendor', ''),
                'qb_amount': None,
                'report_amount': d(ri['amount']),
                'match': False,
            })

    # Sort by date
    def sort_key(r):
        d_str = r['date']
        m = re.match(r'(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?', d_str)
        if not m:
            return (9999, 12, 31)
        mo, dy = int(m.group(1)), int(m.group(2))
        yr = m.group(3) or '00'
        yr = int(yr) if len(yr) == 4 else 2000 + int(yr)
        return (yr, mo, dy)

    rows.sort(key=sort_key)

    # Build markdown
    lines = [f'## {section_name}', '',
             '| DATE | VENDOR | QB AMOUNT | REPORT AMOUNT | MATCH? |',
             '|---|---|---|---|---|']

    qb_total = Decimal('0')
    report_total = Decimal('0')

    for r in rows:
        qb_amt = f'${r["qb_amount"]:,.2f}' if r['qb_amount'] is not None else '—'
        rp_amt = f'${r["report_amount"]:,.2f}' if r['report_amount'] is not None else '—'
        mark = '✅' if r['match'] else '❌'
        lines.append(f'| {r["date"]} | {r["vendor"]} | {qb_amt} | {rp_amt} | {mark} |')
        if r['qb_amount'] is not None:
            qb_total += r['qb_amount']
        if r['report_amount'] is not None:
            report_total += r['report_amount']

    total_match = '✅' if abs(qb_total - report_total) < Decimal('0.02') else '❌'
    lines.append(f'|  | **TOTAL** | **${qb_total:,.2f}** | **${report_total:,.2f}** | {total_match} |')
    lines.append('')
    return '\n'.join(lines), qb_total, report_total


# ── main compare function ─────────────────────────────────────────────────────

def compare(qb_data, recon_data):
    output = []

    period = qb_data.get('period', 'Unknown')
    output.append('=' * 80)
    output.append(f'# QA RECONCILIATION COMPARISON — Period: {period}')
    output.append('=' * 80)
    output.append('')

    # Charges table
    qb_charges = qb_data.get('charges', [])
    # Only compare CHECKED items
    qb_charges_checked = [q for q in qb_charges if q.get('checked')]
    report_charges = recon_data.get('charges', [])

    charges_table, qb_chg_total, rp_chg_total = build_section_table(
        'CHARGES & CASH ADVANCES', qb_charges_checked, report_charges)
    output.append(charges_table)

    # Payments & Credits table  (combine report payments + credits)
    qb_pc = qb_data.get('payments_credits', [])
    qb_pc_checked = [q for q in qb_pc if q.get('checked')]
    report_pc = recon_data.get('credits', []) + recon_data.get('payments', [])

    pc_table, qb_pc_total, rp_pc_total = build_section_table(
        'PAYMENTS & CREDITS', qb_pc_checked, report_pc)
    output.append(pc_table)

    # Summary
    output.append('=' * 80)
    output.append('## SUMMARY')
    output.append('=' * 80)
    output.append('')

    stmt_beg = recon_data.get('beginning_balance')
    stmt_end = recon_data.get('ending_balance')
    qb_beg = d(qb_data.get('beginning_balance', 0))
    qb_end = d(qb_data.get('ending_balance', 0))
    qb_diff = d(qb_data.get('difference', 0))

    output.append('| ITEM | QB AMOUNT | REPORT AMOUNT | MATCH? |')
    output.append('|---|---|---|---|')

    def row(lbl, qv, sv):
        ok = sv is not None and abs(sv - qv) < Decimal('0.02')
        sv_s = f'${sv:,.2f}' if sv is not None else 'N/A'
        return f'| {lbl} | ${qv:,.2f} | {sv_s} | {"✅" if ok else "❌"} |'

    output.append(row('Beginning Balance', qb_beg, stmt_beg))
    output.append(row('Charges',           qb_chg_total, rp_chg_total))
    output.append(row('Payments & Credits', qb_pc_total, rp_pc_total))
    output.append(row('Ending Balance',    qb_end, stmt_end))
    output.append(f'| QB Difference | ${qb_diff:,.2f} | — | {"✅ BALANCED" if abs(qb_diff) < Decimal("0.02") else "❌ OFF"} |')
    output.append('')

    # List issues
    issues = []
    for r in (recon_data.get('charges', []) + recon_data.get('credits', []) +
              recon_data.get('payments', [])):
        if not r.get('matched'):
            issues.append(
                f"MISSING FROM QB: {r['date']} {r['vendor']} ${r['amount']:,.2f}"
            )

    if issues:
        output.append('### Issues Found:')
        output.append('')
        for issue in issues:
            output.append(f'- {issue}')
        output.append('')

    if abs(qb_diff) >= Decimal('0.02'):
        output.append(f'### QB Reconciliation Status: ❌ OFF by ${abs(qb_diff):,.2f}')
    else:
        output.append('### QB Reconciliation Status: ✅ BALANCED')
    output.append('')

    return '\n'.join(output)


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    pdf_path = sys.argv[1]

    if sys.argv[2] == '--json':
        qb_data = json.loads(sys.argv[3])
    else:
        with open(sys.argv[2]) as f:
            qb_data = json.load(f)

    print(f"Running reconciliation on {pdf_path}...", file=sys.stderr)
    recon_text = run_reconciliation(pdf_path)
    recon_data = parse_recon_output(recon_text)

    print(compare(qb_data, recon_data))


if __name__ == '__main__':
    main()
