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

from parsers.base import StatementParser, _registry, KNOWN_CLIENTS
from parsers.report import *
from parsers.report import (
    _safe_date_key, _report_header, _summary_block, _balance_check,
    _payments_section, _credits_section, _individual_section,
    _deposits_section, _checks_section, _adp_section,
    _cc_payments_section, _add_missing_row, _charges_section
)

class USBankCheckingParser(StatementParser):
    """
    U.S. Bank Silver Business Checking (and other USB Business Checking tiers).

    Statement format:
      - Account Summary block with Beginning Balance / Other Deposits /
        Other Withdrawals / Ending Balance
      - "Other Deposits" section: lines like
            "Jan  7  Electronic Deposit  From <VENDOR>   $ 429.66"
      - "Other Withdrawals" section: lines like
            "Jan  7  Electronic Withdrawal  To <VENDOR>   $ 741.99-"
        plus occasional "Analysis Service Charge" lines.
    Each transaction is a single line; amounts on withdrawals have a trailing '-'.
    """
    statement_type = "U.S. Bank Business Checking"

    def __init__(self, pdf_path, client_name=None):
        super().__init__(pdf_path, client_name)
        self.account_number = ''
        self.statement_date = ''
        self.closing_date = None
        self.beginning_balance = None
        self.ending_balance = None
        self.deposits = []
        self.withdrawals = []
        self.service_charge = Decimal('0')
        self.statement_year = None

    def _detect_client(self):
        # For bank statements, match the account holder name from the header only
        # (first 2500 chars) to avoid false positives from payees/vendors in the
        # transaction list. No full-text fallback.
        header = self.text[:2500].upper()
        for name in KNOWN_CLIENTS:
            if name.upper() in header:
                return name
        return None

    def normalize_vendor(self, description):
        result = _registry.normalize_vendor(self.client_name or '', description)
        if result != description:
            return result
        return description.strip()

    def parse(self):
        lines = self.text.split('\n')

        # Account number
        m = re.search(r'Account Number:\s*([\d\s\-]+)', self.text)
        if m:
            self.account_number = re.sub(r'\s+', '-', m.group(1).strip())

        # Statement period — used to resolve "Jan 7" -> "01/07/26"
        # pdftotext -layout places dates in separate columns far from "Statement Period:"
        # Strategy: find the start date from "Beginning Balance on <Month> <D>" and
        # end date from "Ending Balance on <Month> <D>, <YYYY>"
        m_start = re.search(r'Beginning Balance on (\w+ \d+)', self.text)
        m_end   = re.search(r'Ending Balance on\s+(\w+ \d+,?\s*\d{4})', self.text)
        if m_start and m_end:
            start_str = m_start.group(1).strip()
            end_str   = re.sub(r'\s+', ' ', m_end.group(1).strip())
            # Add year to start date using year from end date
            yr_m = re.search(r'(\d{4})', end_str)
            yr = yr_m.group(1) if yr_m else ''
            if yr:
                self.statement_year = int(yr)
            self.statement_date = f"{start_str}, {yr} - {end_str}"
            # Set closing_date to MM/DD/YY for the digest log
            try:
                from datetime import datetime as _dt
                self.closing_date = _dt.strptime(end_str.strip(), '%b %d, %Y').strftime('%m/%d/%y')
            except Exception:
                try:
                    self.closing_date = _dt.strptime(end_str.strip(), '%B %d, %Y').strftime('%m/%d/%y')
                except Exception:
                    self.closing_date = None

        # Beginning / Ending balance
        m = re.search(r'Beginning Balance on \w+ \d+\s+\$?\s*([\d,]+\.\d{2})', self.text)
        if m:
            self.beginning_balance = Decimal(m.group(1).replace(',', ''))
        m = re.search(r'Ending Balance on\s+\w+ \d+,?\s*\d{4}\s+\$?\s*([\d,]+\.\d{2})', self.text)
        if m:
            self.ending_balance = Decimal(m.group(1).replace(',', ''))

        # Transaction parsing. Format per line:
        #   "Jan  7  Electronic Deposit From CONTOSO, INC $ 429.66"
        #   "Jan  7  Electronic Withdrawal To FABRIKAM INC. $ 741.99-"
        #   "Jan 15  Analysis Service Charge  1500000000  6.50-"
        month_map = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,
                     'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}

        txn_re = re.compile(
            r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})\s+(.+?)\s+\$?\s*([\d,]+\.\d{2})(-?)\s*$'
        )

        # Track section context. The Balance Summary at the end of the statement
        # contains lines like "Jan 7 35,046.99  Jan 15  38,172.11" that look like
        # transactions but are running balances — we must skip them.
        in_balance_summary = False
        # Also track when we're past the transactions (Analysis Service Charge Detail, etc.)
        past_transactions = False
        in_checks_section = False

        # Parse checks from "Checks Presented Conventionally" section separately
        checks_re = re.compile(
            r'^(\d+)\s+(\w+ \d+)\s+\d+\s+([\d,]+\.\d{2})\s*$'
        )

        for line in lines:
            stripped = line.strip()

            # Section delimiters
            # Match "Balance Summary" only as a standalone header line (short line),
            # not inside long disclosure paragraphs like "Reserve Line Balance Summary section".
            if stripped == 'Balance Summary' or re.match(r'^Balance Summary\s*$', stripped):
                in_balance_summary = True
                continue
            if 'ANALYSIS SERVICE CHARGE DETAIL' in stripped.upper() or \
               stripped.startswith('Service Activity Detail'):
                past_transactions = True
                continue
            # End of Balance Summary -> resume parsing only if we see a real header again
            # (we don't; the doc ends after Balance Summary on this format)

            # Checks Presented Conventionally section
            if 'Checks Presented Conventionally' in stripped or 'CHECKS PRESENTED CONVENTIONALLY' in stripped.upper():
                in_checks_section = True
                continue
            if in_checks_section:
                # Format: "10287   Mar 23   8055445952   775.58"
                ck = re.match(r'^(\d+)\s+(\w+\s+\d+)\s+\S+\s+([\d,]+\.\d{2})\s*$', stripped)
                if ck:
                    mon_day = ck.group(2).strip()
                    amt = Decimal(ck.group(3).replace(',', ''))
                    try:
                        mo, dy = mon_day.split()
                        year_short = str((self.statement_year or 2026) % 100).zfill(2)
                        chk_date = f"{month_map[mo]:02d}/{int(dy):02d}/{year_short}"
                    except Exception:
                        chk_date = date if date else '00/00/00'
                    self.withdrawals.append({
                        'date': chk_date,
                        'vendor': f'Check #{ck.group(1)}',
                        'amount': amt,
                    })
                continue

            if in_balance_summary or past_transactions:
                continue

            m = txn_re.match(stripped)
            if not m:
                continue
            mon_name, day, desc, amount_str, neg = m.groups()
            year_short = str((self.statement_year or 2026) % 100).zfill(2)
            date = f"{month_map[mon_name]:02d}/{int(day):02d}/{year_short}"
            amount = Decimal(amount_str.replace(',', ''))

            # Clean description
            desc = desc.strip()

            # Withdrawal (negative amount)
            if neg == '-':
                # Special: Analysis Service Charge -> service_charge bucket
                if 'Analysis Service Charge' in desc:
                    self.service_charge += amount
                    # Strip the trailing ref number from the description
                    clean_desc = re.sub(r'\s+\d{8,}\s*$', '', desc).strip()
                    self.withdrawals.append({
                        'date': date,
                        'vendor': self.normalize_vendor(clean_desc) or 'Analysis Service Charge',
                        'amount': amount,
                    })
                    continue
                # Clean common prefixes
                clean = re.sub(r'^Electronic Withdrawal\s+To\s+', '', desc, flags=re.IGNORECASE).strip()
                vendor = self.normalize_vendor(clean) or clean

                # Heuristic: small internal-transfer ADP debits are payroll fees.
                # The client config can opt in via small_transfer_fee_rules.
                cfg = _registry.get_config(self.client_name) or {}
                for rule in cfg.get('small_transfer_fee_rules', []):
                    keyword = rule.get('contains', '').upper()
                    threshold = Decimal(str(rule.get('under_amount', 0)))
                    label = rule.get('normalize_to', vendor)
                    if keyword and keyword in clean.upper() and amount < threshold:
                        vendor = label
                        break

                self.withdrawals.append({
                    'date': date,
                    'vendor': vendor,
                    'amount': amount,
                })
            else:
                # Deposit (positive amount)
                # Skip the summary lines like "Other Deposits 6 75,219.42"
                if re.match(r'^(Other Deposits|Other Withdrawals|Total Other|Number of Days)', desc, re.IGNORECASE):
                    continue
                clean = re.sub(r'^Electronic Deposit\s+From\s+', '', desc, flags=re.IGNORECASE).strip()
                self.deposits.append({
                    'date': date,
                    'vendor': self.normalize_vendor(clean) or clean,
                    'amount': amount,
                })

    def generate_report(self, check_payee_map=None, check_date_map=None):
        check_payee_map  = check_payee_map  or {}
        check_date_map   = check_date_map   or {}
        agg  = lambda txns: self._aggregate_by_vendor(txns, date_fmt='%m/%d/%y')
        norm = self.normalize_vendor

        # Split withdrawals: payroll, cc payments, checks, other — mirrors BofA standard
        cfg = _registry.get_config(self.client_name) or {}
        payroll_kws = [kw.upper() for kw in cfg.get('payroll_vendors', [])]
        cc_kws      = [kw.upper() for kw in cfg.get('cc_payment_vendors', [])]

        def is_payroll_check(num):
            return bool(re.match(r'^10\d{3}$', str(num)))

        payroll_txns   = [w for w in self.withdrawals
                          if any(kw in w['vendor'].upper() for kw in payroll_kws)]
        cc_txns        = [w for w in self.withdrawals
                          if any(kw in w['vendor'].upper() for kw in cc_kws)
                          and w not in payroll_txns]
        check_txns     = [w for w in self.withdrawals
                          if w['vendor'].startswith('Check #')
                          and not is_payroll_check(w['vendor'].replace('Check #','').strip())]
        payroll_checks = [w for w in self.withdrawals
                          if w['vendor'].startswith('Check #')
                          and is_payroll_check(w['vendor'].replace('Check #','').strip())]
        other_txns     = [w for w in self.withdrawals
                          if w not in payroll_txns and w not in cc_txns
                          and w not in check_txns and w not in payroll_checks]

        pay_rows = sorted(
            [{'date': t['date'], 'vendor': norm(t['vendor']),
              'amount': Decimal(str(t['amount'])), 'count': 1}
             for t in payroll_txns + payroll_checks],
            key=lambda x: _safe_date_key(x['date'])
        )
        cc_rows = sorted(
            [{'date': t['date'], 'vendor': norm(t['vendor']),
              'amount': Decimal(str(t['amount']))}
             for t in cc_txns],
            key=lambda x: _safe_date_key(x['date'])
        )
        other_rows = sorted(
            [{'date': t['date'], 'vendor': norm(t['vendor']),
              'amount': Decimal(str(t['amount'])), 'count': 1}
             for t in other_txns],
            key=lambda x: _safe_date_key(x['date'])
        )
        # Deposits: never aggregate — each is a distinct revenue event
        dep_rows = sorted(
            [{'date': d['date'], 'vendor': norm(d['vendor']),
              'amount': Decimal(str(d['amount'])), 'count': 1}
             for d in self.deposits],
            key=lambda x: _safe_date_key(x['date'])
        )

        total_deposits    = sum(d['amount'] for d in self.deposits)
        total_pay         = sum(r['amount'] for r in pay_rows)
        total_cc          = sum(r['amount'] for r in cc_rows)
        total_other       = sum(r['amount'] for r in other_rows)
        total_checks      = sum(Decimal(str(w['amount'])) for w in check_txns)
        total_withdrawals = total_pay + total_cc + total_other + total_checks

        report = _report_header(
            self.statement_type, self.client_name,
            account_number=self.account_number or None,
            statement_date=self.statement_date or None,
            account_label='Account Number',
        )

        summary_rows = [
            ('Beginning Balance',       self.beginning_balance),
            ('Deposits and Credits',    total_deposits),
            ('Withdrawals and Debits',  total_withdrawals if total_withdrawals else None),
            ('  Checks',               total_checks if total_checks else None, 'indent'),
            ('  Payroll',              total_pay    if total_pay    else None, 'indent'),
            ('  Credit Card Payments', total_cc     if total_cc     else None, 'indent'),
            ('Ending Balance',          self.ending_balance),
        ]
        report += _summary_block(summary_rows)

        if self.beginning_balance is not None and self.ending_balance is not None:
            calc = self.beginning_balance + total_deposits - total_withdrawals
            ok = abs(calc - self.ending_balance) < Decimal('0.01')
            report += _balance_check(ok, calc)

        if dep_rows:
            report += _deposits_section(dep_rows, total_deposits)
        if other_rows:
            agg_other_rows = agg(other_txns)
            total_other = sum(r['amount'] for r in agg_other_rows)
            paired_vendors = cfg.get('paired_debit_vendors', [])
            report += _charges_section(agg_other_rows, total_other, title='WITHDRAWALS AND DEBITS',
                                       paired_vendors=paired_vendors)
        if check_txns:
            report += _checks_section(check_txns, total_checks)
        if pay_rows:
            report += _adp_section(pay_rows, total_pay)
        if cc_rows:
            report += _cc_payments_section(cc_rows, total_cc)

        return report


