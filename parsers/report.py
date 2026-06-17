import sys
import re
import os
import json
from pathlib import Path
from decimal import Decimal
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

def _now_pst():
    """Return current datetime in US/Pacific (PST/PDT)."""
    return datetime.now(ZoneInfo('America/Los_Angeles'))

try:
    import fitz
    import pytesseract
    from PIL import Image
    import io as _io
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

from parsers.base import _now_pst

def _safe_date_key(d):
    for fmt in ('%m/%d/%y', '%m/%d/%Y', '%m/%d'):
        try:
            return datetime.strptime(d, fmt)
        except ValueError:
            pass
    return datetime(2000, 1, 1)


def _report_header(statement_type, client_name=None, account_number=None,
                   statement_date=None, account_label='Account'):
    lines = ['=' * 80, statement_type.upper()]
    if client_name:
        lines.append(client_name)
    if account_number:
        lines.append(f'{account_label}: {account_number}')
    if statement_date:
        lines.append(f'Statement Period: {statement_date}')
    lines.append(f"Generated: {_now_pst().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append('=' * 80)
    lines.append('')
    return '\n'.join(lines) + '\n'


def _summary_block(rows):
    """
    Render the statement summary block.
    Each row is (label, value) or (label, value, 'indent') for sub-items.
    Indented rows are shown with 2-space indent and smaller label width.
    """
    lines = ['STATEMENT SUMMARY']
    for row in rows:
        label, value = row[0], row[1]
        indented = len(row) > 2 and row[2] == 'indent'
        if value is None:
            continue
        if label in ('Beginning Balance', 'Ending Balance', 'Previous Balance', 'New Balance'):
            lines.append('-' * 80)
            formatted_value = f"${value:>12,.2f}"
            if label == 'New Balance' and value < 0:
                # ANSI red for negative new balance
                formatted_value = f"\033[91m{formatted_value}\033[0m"
            lines.append(f"{label + ':':<40} {formatted_value}")
            lines.append('-' * 80)
        elif indented:
            lines.append(f"  {label + ':':<38} ${value:>12,.2f}")
        else:
            lines.append(f"{label + ':':<40} ${value:>12,.2f}")
    lines.append('')
    return '\n'.join(lines) + '\n'


def _balance_check(ok, calc=None):
    if ok:
        return "✓ Balance verification: PASSED\n\n"
    else:
        return f"✗ Balance verification: FAILED  (Calculated ${calc:,.2f})\n\n"


def _payments_section(payments, total_payments):
    lines = ['=' * 80, 'PAYMENTS', '',
             f"{'Date':<12} {'Description':<50} {'Amount':>16}",
             '-' * 80]
    for p in sorted(payments, key=lambda x: _safe_date_key(x['date'])):
        lines.append(f"{p['date']:<12} {p['description']:<50} ${p['amount']:>15,.2f}")
    lines.append(f"{'':63} {'-' * 17}")
    lines.append(f"{'TOTAL PAYMENTS:':<63} ${total_payments:>15,.2f}")
    lines.append('')
    return '\n'.join(lines) + '\n'


def _credits_section(credits, total_credits, title='CREDITS / RETURNS'):
    lines = ['=' * 80, title, '',
             f"{'Date':<12} {'Description':<50} {'Amount':>16}",
             '-' * 80]
    for c in sorted(credits, key=lambda x: _safe_date_key(x['date'])):
        lines.append(f"{c['date']:<12} {c['description']:<50} ${c['amount']:>15,.2f}")
    lines.append(f"{'':63} {'-' * 17}")
    lines.append(f"{'TOTAL CREDITS / RETURNS:':<63} ${total_credits:>15,.2f}")
    lines.append('')
    return '\n'.join(lines) + '\n'


def _individual_section(rows, total, title):
    """Generic section with Date / Description / Amount columns — no aggregation."""
    W = 80
    lines = ['=' * W, title, '',
             f"{'Date':<12} {'Description':<50} {'Amount':>16}",
             '-' * W]
    for r in rows:
        lines.append(f"{r['date']:<12} {r['vendor']:<50} ${r['amount']:>15,.2f}")
    lines.append(f"{'':63} {'-' * 17}")
    lines.append(f"{'TOTAL ' + title + ':':<63} ${total:>15,.2f}")
    lines.append('')
    return '\n'.join(lines) + '\n'


def _deposits_section(credits, total_credits, title='CREDITS / DEPOSITS', account_label=None):
    lines = ['=' * 80, title, '',
             f"{'Date':<12} {'Description':<50} {'Amount':>16}",
             '-' * 80]
    for c in sorted(credits, key=lambda x: _safe_date_key(x['date'])):
        count_str = f" ({c['count']})" if c.get('count', 1) > 1 else ''
        label_str = f" ({account_label})" if account_label else ''
        vendor_display = f"{c['vendor']}{count_str}{label_str}"
        lines.append(f"{c['date']:<12} {vendor_display:<50} ${c['amount']:>15,.2f}")
    lines.append(f"{'':63} {'-' * 17}")
    lines.append(f"{'TOTAL CREDITS:':<63} ${total_credits:>15,.2f}")
    lines.append('')
    return '\n'.join(lines) + '\n'


def _checks_section(checks, total_checks):
    lines = ['=' * 80, 'CHECKS', '',
             f"{'Check #':<10} {'Date':<12} {'Payee':<42} {'Amount':>13}",
             '-' * 80]
    def _check_sort_key(ck):
        num = ck.get('check_num') or ck.get('check_number', '0')
        return int(num) if num.isdigit() else 0
    for ck in sorted(checks, key=_check_sort_key):
        check_num = ck.get('check_num') or ck.get('check_number', '')
        payee = ck.get('payee', '') or ''
        lines.append(f"{check_num:<10} {ck['date']:<12} {payee[:42]:<42} ${Decimal(str(ck['amount'])):>13,.2f}")
    lines.append(f"{'':63} {'-' * 15}")
    lines.append(f"{'TOTAL CHECKS:':<63} ${total_checks:>13,.2f}")
    lines.append('')
    return '\n'.join(lines) + '\n'


def _adp_section(adp_transactions, total_adp):
    lines = ['=' * 80, 'PAYROLL', '',
             f"{'Date':<12} {'Description':<50} {'Amount':>16}",
             '-' * 80]
    for a in adp_transactions:
        lines.append(f"{a['date']:<12} {a['vendor']:<50} ${Decimal(str(a['amount'])):>15,.2f}")
    lines.append(f"{'':63} {'-' * 17}")
    lines.append(f"{'TOTAL PAYROLL:':<63} ${total_adp:>15,.2f}")
    lines.append('')
    return '\n'.join(lines) + '\n'


def _cc_payments_section(cc_payments, total_cc, title='CREDIT CARD PAYMENTS',
                         group_vendor_keywords=None):
    """
    Render CC payments sorted by vendor bucket first, then by date descending within
    each bucket. This keeps all Citi payments together, all BofA payments together, etc.

    group_vendor_keywords: optional list of keyword strings used to bucket vendors.
    If None, sort is simply by date ascending (legacy behaviour).
    """
    lines = ['=' * 80, title, '',
             f"{'Date':<12} {'Description':<50} {'Amount':>16}",
             '-' * 80]

    if group_vendor_keywords:
        # Assign each payment a bucket based on first matching keyword
        def _bucket(vendor):
            vu = vendor.upper()
            for i, kw in enumerate(group_vendor_keywords):
                if kw.upper() in vu:
                    return i
            return len(group_vendor_keywords)  # unknown — goes last

        sorted_payments = sorted(
            cc_payments,
            key=lambda x: (_bucket(x['vendor']), _safe_date_key(x['date']))
        )
    else:
        sorted_payments = sorted(cc_payments, key=lambda x: _safe_date_key(x['date']))

    for p in sorted_payments:
        pending_flag = ' ⏳' if p.get('pending') else ''
        vendor_str = f"{p['vendor']}{pending_flag}"
        lines.append(f"{p['date']:<12} {vendor_str:<50} ${Decimal(str(p['amount'])):>15,.2f}")
    lines.append(f"{'':63} {'-' * 17}")
    lines.append(f"{'TOTAL ' + title + ':':<63} ${total_cc:>15,.2f}")
    lines.append('')
    return '\n'.join(lines) + '\n'


def _add_missing_row(aggregated, total_charges, statement_charges):
    """
    If captured charges don't match statement charges, prepend a MISSING row
    showing the unrecovered difference so totals balance to the statement.
    Returns updated (aggregated, total_charges).
    """
    if statement_charges is None:
        return aggregated, total_charges
    diff = statement_charges - total_charges
    if abs(diff) < Decimal('0.01'):
        return aggregated, total_charges
    missing_row = {
        'date': '??/??/??',
        'vendor': '*** MISSING — enter manually ***',
        'amount': diff,
        'count': 1,
    }
    return [missing_row] + list(aggregated), statement_charges


def _charges_section(aggregated, total_charges, title='CHARGES', paired_vendors=None):
    """
    paired_vendors: list of dicts with keys: contains, debit_account, credit_account.
    When a row's vendor matches, render two indented QB account sub-lines (DR/CR)
    beneath the transaction line.
    """
    paired_vendors = paired_vendors or []
    lines = ['=' * 80, title, '',
             f"{'Date':<12} {'Description':<50} {'Amount':>16}",
             '-' * 80]
    # Group paired-vendor rows by (vendor, date) so same-date entries share one header
    from collections import OrderedDict
    paired_keys = []
    paired_groups = OrderedDict()
    non_paired = []
    for row in aggregated:
        vendor_clean = row['vendor'].split('|')[0]
        pair = next((p for p in paired_vendors
                     if p.get('contains', '').upper() in vendor_clean.upper()), None)
        if pair:
            key = (row['date'], vendor_clean, pair['debit_account'], pair['credit_account'])
            if key not in paired_groups:
                paired_keys.append(key)
                paired_groups[key] = []
            paired_groups[key].append(row['amount'])
        else:
            non_paired.append(row)

    # Rebuild aggregated preserving original sort order, replacing paired rows with grouped versions
    rendered_paired_keys = set()
    for row in aggregated:
        vendor_clean = row['vendor'].split('|')[0]
        count_str = f" ({row['count']})" if row['count'] > 1 else ''
        vendor_display = f"{vendor_clean}{count_str}"
        pair = next((p for p in paired_vendors
                     if p.get('contains', '').upper() in vendor_clean.upper()), None)
        if pair:
            key = (row['date'], vendor_clean, pair['debit_account'], pair['credit_account'])
            if key in rendered_paired_keys:
                continue  # already rendered this date+vendor group
            rendered_paired_keys.add(key)
            lines.append(f"{row['date']:<12} {vendor_clean[:50]}")
            # Every 2 transactions = 1 DR/CR pair, each at the individual transaction amount
            amounts = paired_groups[key]
            for i in range(0, len(amounts), 2):
                amt = amounts[i]
                lines.append(f"{'':12}   DR  {pair['debit_account']:<44} ${amt:>15,.2f}")
                lines.append(f"{'':12}   CR  {pair['credit_account']:<44} ${amt:>15,.2f}")
        else:
            lines.append(f"{row['date']:<12} {vendor_display[:50]:<50} ${row['amount']:>15,.2f}")
    lines.append(f"{'':63} {'-' * 17}")
    lines.append(f"{'TOTAL CHARGES:':<63} ${total_charges:>15,.2f}")
    lines.append('')
    return '\n'.join(lines) + '\n'


# ═══════════════════════════════════════════════════════════════════════════════
# AUTO-DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

