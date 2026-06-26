import sys
import re
import os
import json
import subprocess
import zipfile
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

from parsers.base import StatementParser, _registry, KNOWN_CLIENTS, CLIENT_CARDHOLDERS, _classify_cc_transaction
from parsers.report import *
from parsers.report import (
    _report_header, _summary_block, _balance_check,
    _deposits_section, _charges_section, _checks_section,
    _adp_section, _payments_section, _credits_section,
    _cc_payments_section, _add_missing_row, _individual_section,
    _safe_date_key,
)

class AmexStatementParser(StatementParser):
    """
    American Express Business statements.
    Handles the zip-of-text-pages format AmEx delivers as well as standard PDFs.
    """
    statement_type = "American Express Business"

    def __init__(self, pdf_path, client_name=None):
        self.pdf_path = str(pdf_path)
        self.text = self._extract_text_amex()
        self.client_name = client_name or self._detect_client()
        self.closing_date = None
        self.account_number = None
        self.previous_balance = None
        self.new_balance = None
        self.payments = []
        self.credits = []
        self.charges = []
        self.fees = Decimal('0')
        self.interest = Decimal('0')

    def _extract_text_amex(self):
        """Try zip-of-pages format first, fall back to pdftotext."""
        try:
            with zipfile.ZipFile(self.pdf_path, 'r') as z:
                txt_files = sorted(
                    [n for n in z.namelist() if n.endswith('.txt')],
                    key=lambda n: int(re.search(r'(\d+)', n).group(1))
                )
                pages = []
                for fname in txt_files:
                    with z.open(fname) as f:
                        pages.append(f.read().decode('utf-8', errors='replace'))
                return '\n'.join(pages)
        except Exception:
            pass
        try:
            result = subprocess.run(
                ['pdftotext', '-layout', self.pdf_path, '-'],
                capture_output=True, text=True, check=True
            )
            return result.stdout
        except Exception as e:
            print(f"Error extracting AmEx text: {e}")
            sys.exit(1)

    def _detect_client(self):
        text_upper = self.text.upper()
        for name in KNOWN_CLIENTS:
            if name in text_upper:
                return name
        return None

    def parse(self):
        lines = self.text.split('\n')

        # Account metadata
        for line in lines:
            if 'Closing Date' in line:
                m = re.search(r'Closing Date\s+(\d{2}/\d{2}/\d{2})', line)
                if m and not self.closing_date:
                    self.closing_date = m.group(1)
            if 'Account Ending' in line and not self.account_number:
                m = re.search(r'Account Ending\s+([\d\-]+)', line)
                if m:
                    self.account_number = m.group(1)

        # Previous / New Balance — grab last occurrence (Account Total section)
        prev_matches = re.findall(r'Previous Balance\s*\r?\n\s*\$([0-9,]+\.\d{2})', self.text)
        if not prev_matches:
            prev_matches = re.findall(r'Previous Balance\s+\$([0-9,]+\.\d{2})', self.text)
        new_matches = re.findall(r'^New Balance\s+\$([0-9,]+\.\d{2})', self.text, re.MULTILINE)
        if not new_matches:
            new_matches = re.findall(r'New Balance\s*\r?\n\s*\$([0-9,]+\.\d{2})', self.text)
        if not new_matches:
            new_matches = re.findall(r'New Balance\s{2,}\$([0-9,]+\.\d{2})', self.text)
        if prev_matches:
            self.previous_balance = Decimal(prev_matches[-1].replace(',', ''))
        if new_matches:
            self.new_balance = Decimal(new_matches[-1].replace(',', ''))

        # Cardholder names for this client (from config) — used to recognize
        # credit lines that lead with a cardholder name. Built as an optional
        # regex group so capture-group numbering stays fixed (group 2 =
        # cardholder) whether or not the client has configured cardholders;
        # with no cardholders the group uses a never-matching pattern.
        _client_cardholders = CLIENT_CARDHOLDERS.get(self.client_name, [])
        _cardholder_inner = (
            '|'.join(re.escape(c) for c in _client_cardholders)
            if _client_cardholders else '(?!)'
        )
        _credit_re = re.compile(
            r'(\d{2}/\d{2}/\d{2})\*?\s+(?:(' + _cardholder_inner + r')\s+)?(.+?)\s*-\$([0-9,]+\.\d{2})',
            re.IGNORECASE
        )

        # Payments and Credits
        for line in lines:
            # Actual payments
            m = re.match(
                r'(\d{2}/\d{2}/\d{2})\*?\s+.+?(?:AUTOPAY PAYMENT RECEIVED|ELECTRONIC PAYMENT RECEIVED|ONLINE PAYMENT|PAYMENT RECEIVED|PAYMENT - THANK YOU).+?-\$([0-9,]+\.\d{2})',
                line, re.IGNORECASE
            )
            if m:
                line_upper = line.upper()
                if 'AUTOPAY' in line_upper:
                    desc = 'AUTOPAY PAYMENT RECEIVED - THANK YOU'
                elif 'ONLINE PAYMENT' in line_upper:
                    desc = 'ONLINE PAYMENT - THANK YOU'
                else:
                    desc = 'PAYMENT RECEIVED - THANK YOU'
                self.payments.append({
                    'date': m.group(1),
                    'description': desc,
                    'amount': Decimal(m.group(2).replace(',', ''))
                })
            # Credits (e.g. AMEX Wireless Credit, refunds, returns)
            # Handle multiple formats:
            # 1. Simple: DATE DESCRIPTION -$AMOUNT
            # 2. With cardholder: DATE CARDHOLDER DESCRIPTION -$AMOUNT
            # Match if it has CREDIT/REFUND/RETURN/WIRELESS keyword OR is preceded by cardholder
            mc = _credit_re.match(line)
            if mc and not m:
                desc = mc.group(3).strip()
                # Only add if it looks like a credit
                if any(keyword in desc.upper() for keyword in ['CREDIT', 'REFUND', 'RETURN', 'WIRELESS']) or mc.group(2):
                    self.credits.append({
                        'date': mc.group(1),
                        'description': desc,
                        'amount': Decimal(mc.group(4).replace(',', ''))
                    })

        # Charges — multi-line: date+desc line, then optional phone/ref lines, then $amount line
        cardholders = CLIENT_CARDHOLDERS.get(self.client_name, [])
        cardholder_pattern = re.compile(
            r'^(' + '|'.join(re.escape(c) for c in cardholders) + r')\s*$'
        ) if cardholders else None

        # Match transactions with amount inline at end of line (e.g. "01/28/26   Extra Space   $38.00 ⧫")
        txn_line_inline = re.compile(
            r'^(\d{2}/\d{2}/\d{2})\*?\s+(.+?)\s+\$([0-9,]+\.\d{2})\s*[⧫\*]?\s*$'
        )
        txn_line = re.compile(r'^(\d{2}/\d{2}/\d{2})\s+(.+)')
        amount_line = re.compile(r'^\$([0-9,]+\.\d{2})')
        skip_keywords = ['ELECTRONIC PAYMENT', 'AUTOPAY PAYMENT', 'Total Fees', 'Total Interest',
                         'Closing Date', 'Account Ending', 'Card Ending',
                         'Customer Care', 'Next Closing', 'AMEX Wireless Credit',
                         'Payments', 'Credits', 'New Charges', 'Total Payments',
                         'Detail', 'Summary', 'Amount']

        current_cardholder = None
        pending_date = None
        pending_vendor = None

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            if cardholder_pattern and cardholder_pattern.match(stripped):
                current_cardholder = stripped
                continue

            # Try inline amount format first (amount at end of same line)
            inline_m = txn_line_inline.match(stripped)
            if inline_m:
                date_str = inline_m.group(1)
                vendor_raw = inline_m.group(2).strip()
                txn_amount_str = inline_m.group(3)
                if any(kw in vendor_raw for kw in skip_keywords):
                    pending_date = None
                    pending_vendor = None
                    continue
                # Annual fees are captured in Finance Charges — skip them here
                # to avoid double-counting
                if 'ANNUAL FEE' in vendor_raw.upper():
                    pending_date = None
                    pending_vendor = None
                    continue
                # Remove trailing ref numbers / extra merchant detail
                vendor_raw = re.sub(r'\s{2,}.*$', '', vendor_raw)
                vendor = self.normalize_vendor(vendor_raw)
                txn_amount = Decimal(txn_amount_str.replace(',', ''))
                txn_type = _classify_cc_transaction(vendor, txn_amount)
                if txn_type == 'credit':
                    self.credits.append({
                        'date': date_str,
                        'description': vendor,
                        'amount': txn_amount,
                    })
                else:
                    self.charges.append({
                        'date': date_str,
                        'cardholder': current_cardholder or '',
                        'vendor': vendor,
                        'amount': txn_amount,
                    })
                pending_date = None
                pending_vendor = None
                continue

            # Fallback: separate-line amount
            amt_m = amount_line.match(stripped)
            if amt_m and pending_date and pending_vendor:
                vendor = self.normalize_vendor(pending_vendor)
                txn_amount = Decimal(amt_m.group(1).replace(',', ''))
                txn_type = _classify_cc_transaction(vendor, txn_amount)
                if txn_type == 'credit':
                    self.credits.append({
                        'date': pending_date,
                        'description': vendor,
                        'amount': txn_amount,
                    })
                else:
                    self.charges.append({
                        'date': pending_date,
                        'cardholder': current_cardholder or '',
                        'vendor': vendor,
                        'amount': txn_amount,
                    })
                pending_date = None
                pending_vendor = None
                continue

            txn_m = txn_line.match(stripped)
            if txn_m:
                if any(kw in txn_m.group(2) for kw in skip_keywords):
                    pending_date = None
                    pending_vendor = None
                    continue
                # Annual fees are captured in Finance Charges — skip them here
                if 'ANNUAL FEE' in txn_m.group(2).upper():
                    pending_date = None
                    pending_vendor = None
                    continue
                pending_date = txn_m.group(1)
                vendor_raw = txn_m.group(2)
                # Normal extraction: remove amount and trailing state codes
                vendor_raw = re.sub(r'\s{2,}.*$', '', vendor_raw)
                vendor_raw = re.sub(r'\s+[A-Z][A-Z\s]+[A-Z]{2}\s*$', '', vendor_raw).strip()
                pending_vendor = vendor_raw

        # Fees / Interest — try the detailed section labels first, then fall back
        # to the summary "Finance Charges" line used on some AMEX statement formats.
        m = re.search(r'Total Fees for this Period\s+\$([0-9,]+\.\d{2})', self.text)
        if m:
            self.fees = Decimal(m.group(1).replace(',', ''))
        m = re.search(r'Total Interest Charged for this Period\s+\$([0-9,]+\.\d{2})', self.text)
        if m:
            self.interest = Decimal(m.group(1).replace(',', ''))
        if self.fees == 0 and self.interest == 0:
            m = re.search(r'Finance Charges[:\s]+\$\s*([0-9,]+\.\d{2})', self.text)
            if m:
                self.fees = Decimal(m.group(1).replace(',', ''))

        # Remove any charge transaction whose amount equals the captured finance-
        # charge total — AMEX sometimes emits these as dated line items in the
        # charges section even though they're already tallied in fees/interest.
        finance_total = self.fees + self.interest
        if finance_total > 0:
            self.charges = [
                c for c in self.charges
                if not (
                    abs(Decimal(str(c['amount'])) - finance_total) < Decimal('0.01')
                    and any(kw in c.get('vendor', '').upper()
                            for kw in ('INTEREST', 'FINANCE', 'PERIODIC', 'FEE', 'CHARGE'))
                )
            ]

    def generate_report(self):
        aggregated = self._aggregate_by_vendor(
            [{'date': c['date'], 'vendor': c['vendor'], 'amount': c['amount']}
             for c in self.charges
             if 'INTEREST' not in c['vendor'].upper()],
            date_fmt='%m/%d/%y'
        )
        total_charges = sum(r['amount'] for r in aggregated)
        total_payments = sum(p['amount'] for p in self.payments)
        total_credits = sum(c['amount'] for c in self.credits)
        statement_charges = None
        if self.new_balance is not None and self.previous_balance is not None:
            statement_charges = (self.new_balance - self.previous_balance
                                 + total_payments + total_credits
                                 - self.fees - self.interest)
        aggregated, total_charges = _add_missing_row(aggregated, total_charges, statement_charges)

        acct = self.account_number if self.account_number else None
        report = _report_header(self.statement_type, self.client_name,
                                account_number=acct,
                                statement_date=self.closing_date,
                                account_label='Account Ending')

        summary_rows = [
            ('Previous Balance',  self.previous_balance),
            ('Payments',          total_payments),
            ('Credits / Returns', total_credits if total_credits else None),
            ('Purchases',       total_charges),
            ('Finance Charges',    self.fees + self.interest if self.fees + self.interest else None),
            ('New Balance',       self.new_balance),
        ]
        report += _summary_block(summary_rows)

        if self.previous_balance is not None and self.new_balance is not None:
            calc = self.previous_balance + total_charges - total_payments - total_credits + self.fees + self.interest
            ok = abs(calc - self.new_balance) < Decimal('0.01')
            report += _balance_check(ok, calc)

        if self.payments:
            report += _payments_section(self.payments, total_payments)
        if self.credits:
            report += _credits_section(self.credits, total_credits)
        report += _charges_section(aggregated, total_charges)
        return report



class AmexCheckingParser(StatementParser):
    """
    American Express Business Checking account statements.

    Statement format:
      - Multi-line transactions with Credits / Debits / Balance columns
      - Date pattern: MM/DD/YYYY  (full year, unlike BofA MM/DD/YY)
      - Credits:  vendor transfers, Wire transfers, Interest deposits
      - Debits:   ADP Wage Pay (→ ADP PAYROLL section, never aggregate),
                  Check withdrawals (→ CHECKS section),
                  All others (→ WITHDRAWALS section)
      - Checks Paid Summary at end of statement lists check# + date + amount
      - Section order: DEPOSITS → WITHDRAWALS → ADP PAYROLL → CHECKS
    """
    statement_type = "American Express Business Checking"

    def __init__(self, pdf_path, client_name=None):
        super().__init__(pdf_path, client_name)
        self.beginning_balance = None
        self.ending_balance = None
        self.statement_date = ''
        self.account_number = ''
        self.credits = []    # list of {date, description, amount}  (excludes interest)
        self.debits = []     # list of {date, description, amount}
        self.checks = []     # list of {date, check_number, amount}
        self.interest_earned = Decimal('0')  # shown separately in summary

    def _detect_client(self):
        lines = self.text.split('\n')
        for line in lines[:30]:
            lu = line.upper().strip()
            for name in KNOWN_CLIENTS:
                if name in lu:
                    return name
        return super()._detect_client()

    def parse(self):
        # Reset state so parse() is idempotent
        self.credits = []
        self.debits = []
        self.checks = []
        self.interest_earned = Decimal('0')
        self.beginning_balance = None
        self.ending_balance = None
        self.statement_date = ''
        self.account_number = ''

        lines = self.text.split('\n')

        # ── metadata ──────────────────────────────────────────────────────────
        for line in lines:
            if 'Beginning Balance as of' in line and self.beginning_balance is None:
                m = re.search(r'\$([0-9,]+\.\d{2})', line)
                if m:
                    self.beginning_balance = Decimal(m.group(1).replace(',', ''))
            if 'Ending Balance as of' in line and self.ending_balance is None:
                m = re.search(r'\$([0-9,]+\.\d{2})', line)
                if m:
                    self.ending_balance = Decimal(m.group(1).replace(',', ''))
            if 'Statement Date:' in line and not self.statement_date:
                m = re.search(r'Statement Date:\s+(\d{2}/\d{2}/\d{4})', line)
                if m:
                    self.statement_date = m.group(1)
            if 'Account Ending:' in line and not self.account_number:
                m = re.search(r'Account Ending:\s+\*?(\d+)', line)
                if m:
                    self.account_number = m.group(1)

        # ── transaction parsing ───────────────────────────────────────────────
        # pdftotext -layout format for AmEx Business Checking:
        #
        #   01/05/2026 Online Transfer / Payment: Credit      $425.00            $100,000.00
        #                EXAMPLE VENDOR TRANSFER *****XXXX
        #                XXXXXXXXXXXXXXX EXAMPLE CLUB
        #                External - BANK OF AMERICA,N.A.
        #                ID 000000000000000
        #
        # The date line contains the transaction type keyword (Credit/Debit) and amounts.
        # Continuation lines (leading spaces) provide vendor detail.
        # Checks Paid Summary at end: "312   01/12/2026   $1,500.00"

        in_checks_summary = False
        skip_keywords = ['Beginning Balance', 'Ending Balance', 'Date         Description',
                         'Continued on next page', 'Account Activity', 'Accounts offered by',
                         'Statement Date:', 'Account Address', 'Contact Us']

        i = 0
        while i < len(lines):
            line = lines[i]

            if 'Checks Paid Summary' in line:
                in_checks_summary = True
                i += 1
                continue

            if in_checks_summary:
                m = re.match(r'^\s*(\d+)\s+(\d{2}/\d{2}/\d{4})\s+\$([0-9,]+\.\d{2})', line)
                if m:
                    self.checks.append({
                        'check_number': m.group(1),
                        'date': self._fmt_date(m.group(2)),
                        'amount': Decimal(m.group(3).replace(',', '')),
                        'payee': '',
                    })
                i += 1
                continue

            # Transaction lines start with MM/DD/YYYY at column 0 (no leading spaces)
            dm = re.match(r'^(\d{2}/\d{2}/\d{4})\s+(.+)', line)
            if not dm:
                i += 1
                continue

            date = self._fmt_date(dm.group(1))
            header = dm.group(2)

            if any(kw in header for kw in skip_keywords):
                i += 1
                continue

            header_upper = header.upper()
            is_interest = 'INTEREST DEPOSIT' in header_upper
            is_credit = (': CREDIT' in header_upper or is_interest or
                         'WIRE TRANSFER DOMESTIC INCOMING' in header_upper)
            is_debit  = (': DEBIT' in header_upper or 'CHECK: WITHDRAWAL' in header_upper)

            # Extract first signed dollar amount from the header line (the txn amount).
            # The last dollar value on the line is always the running balance — skip it.
            signed_amounts = re.findall(r'(-?\$[0-9,]+\.\d{2})', header)
            if not signed_amounts:
                i += 1
                continue

            raw_val = signed_amounts[0].replace('$', '').replace(',', '')
            try:
                txn_amount = Decimal(raw_val)
            except Exception:
                i += 1
                continue

            # Collect indented continuation lines for vendor description
            vendor_parts = []
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if nxt and nxt[0] == ' ':
                    s = nxt.strip()
                    if s and not s.startswith('ID ') and not re.match(r'^[A-Z]{2,}\d{6,}', s):
                        vendor_parts.append(s)
                    j += 1
                else:
                    break

            # Build description: prefer first continuation line, fall back to header type label
            if vendor_parts:
                desc = vendor_parts[0]
            elif is_interest:
                desc = 'Interest Deposit'
            else:
                # Strip amounts from header to get the label
                desc = re.sub(r'\s+-?\$[0-9,]+\.\d{2}.*$', '', header).strip()

            # Route to correct bucket
            if is_credit or (not is_debit and txn_amount > 0):
                amount = abs(txn_amount)
                if is_interest:
                    self.interest_earned += amount
                else:
                    self.credits.append({'date': date, 'description': desc, 'amount': amount})
            else:
                amount = abs(txn_amount)
                self.debits.append({'date': date, 'description': desc, 'amount': amount})

            i = j

        # Remove check debits — they appear in Checks Paid Summary and are
        # fully accounted for in the CHECKS section. Filter by description keyword.
        self.debits = [d for d in self.debits
                       if 'CHECK' not in d['description'].upper()]

    def _fmt_date(self, date_str):
        """Convert MM/DD/YYYY → MM/DD/YY for consistency with other parsers."""
        try:
            return datetime.strptime(date_str, '%m/%d/%Y').strftime('%m/%d/%y')
        except ValueError:
            return date_str

    def aggregate_transactions(self):
        """
        Separate into sections:
          DEPOSITS   - credits aggregated by vendor at latest date; any
                       configured roll-up vendors collapsed to one line each
          WITHDRAWALS - ADP → ADP PAYROLL (never aggregate);
                        credit card payments → individual lines (never aggregate);
                        configured roll-up vendor debit(s) → single line each;
                        all other vendors → aggregated by vendor at latest date
          CHECKS     - from Checks Paid Summary (never aggregate)
        """
        # ── credits ───────────────────────────────────────────────────────────
        # Internal transfers (e.g. between a client's own entities) are listed
        # individually; the match strings come from config so no counterparty
        # names live in code (config: internal_transfer_keywords).
        cfg = _registry.get_config(self.client_name) or {}
        internal_kw = [k.upper() for k in (cfg.get('internal_transfer_keywords') or [])]
        aggs = self.transaction_aggregations()
        agg_credits = {a['card_label']: [] for a in aggs}
        internal_transfer_credits = []
        other_credits = []
        for t in self.credits:
            du = t['description'].upper()
            rule = next((a for a in aggs if a['match'] in du), None)
            if rule:
                agg_credits[rule['card_label']].append(t)
            elif internal_kw and any(k in du for k in internal_kw):
                internal_transfer_credits.append(t)
            else:
                other_credits.append(t)

        # Aggregate other credits by vendor at latest date
        credit_totals = defaultdict(lambda: {'total': Decimal('0'), 'count': 0, 'latest_date': None})
        for t in other_credits:
            v = self.normalize_vendor(t['description'])
            credit_totals[v]['total'] += t['amount']
            credit_totals[v]['count'] += 1
            d = datetime.strptime(t['date'], '%m/%d/%y')
            if credit_totals[v]['latest_date'] is None or d > credit_totals[v]['latest_date']:
                credit_totals[v]['latest_date'] = d

        deposits = [
            {'date': data['latest_date'].strftime('%m/%d/%y'), 'vendor': v,
             'amount': data['total'], 'count': data['count']}
            for v, data in credit_totals.items()
        ]

        # Internal transfers: one line per transaction (not grouped)
        for t in internal_transfer_credits:
            deposits.append({'date': t['date'], 'vendor': self.normalize_vendor(t['description']),
                             'amount': t['amount'], 'count': 1})

        for label, txns in agg_credits.items():
            if txns:
                deposits.append(self._rollup_line(txns, label))

        deposits.sort(key=lambda x: datetime.strptime(x['date'], '%m/%d/%y'))

        # ── debits ────────────────────────────────────────────────────────────
        adp_transactions = []
        cc_payments = []
        agg_debits = {a['card_label']: [] for a in aggs}
        other_debits = []

        for t in self.debits:
            d_upper = t['description'].upper()
            if 'ADP' in d_upper:
                adp_transactions.append({'date': t['date'], 'vendor': t['description'],
                                         'amount': t['amount'], 'count': 1})
            elif ('AMEX EPAYMENT' in d_upper or 'CHASE CREDIT CRD' in d_upper or
                  'CREDIT CARD' in d_upper or 'AUTOPAY' in d_upper or
                  any(k.upper() in d_upper for k in (cfg.get('cc_keywords') or []))):
                cc_payments.append({'date': t['date'], 'vendor': self.normalize_vendor(t['description']),
                                    'amount': t['amount'], 'count': 1})
            else:
                rule = next((a for a in aggs if a['match'] in d_upper), None)
                if rule:
                    agg_debits[rule['card_label']].append(t)
                else:
                    other_debits.append(t)

        # Aggregate other debits by vendor at latest date
        debit_totals = defaultdict(lambda: {'total': Decimal('0'), 'count': 0, 'latest_date': None})
        for t in other_debits:
            v = self.normalize_vendor(t['description'])
            debit_totals[v]['total'] += t['amount']
            debit_totals[v]['count'] += 1
            d = datetime.strptime(t['date'], '%m/%d/%y')
            if debit_totals[v]['latest_date'] is None or d > debit_totals[v]['latest_date']:
                debit_totals[v]['latest_date'] = d

        withdrawals = [
            {'date': data['latest_date'].strftime('%m/%d/%y'), 'vendor': v,
             'amount': data['total'], 'count': data['count']}
            for v, data in debit_totals.items()
        ]

        # Credit card payments: individual, sorted by date
        cc_payments.sort(key=lambda x: datetime.strptime(x['date'], '%m/%d/%y'))
        withdrawals.extend(cc_payments)

        withdrawals.sort(key=lambda x: datetime.strptime(x['date'], '%m/%d/%y'))

        for label, txns in agg_debits.items():
            if txns:
                withdrawals.append(self._rollup_line(txns, label))

        withdrawals.sort(key=lambda x: datetime.strptime(x['date'], '%m/%d/%y'))

        checks_sorted = sorted(self.checks,
                               key=lambda x: int(x['check_number']) if x['check_number'].isdigit() else 0,
                               reverse=True)

        return deposits, withdrawals, adp_transactions, checks_sorted

    def generate_report(self, check_payee_map=None, check_date_map=None):
        self.parse()
        deposits, withdrawals, adp, checks = self.aggregate_transactions()

        total_deposits    = sum(d['amount'] for d in deposits)
        total_withdrawals = sum(w['amount'] for w in withdrawals)
        total_adp         = sum(a['amount'] for a in adp)
        total_checks      = sum(Decimal(str(c['amount'])) for c in checks)
        total_debits      = total_withdrawals + total_adp + total_checks

        period = self.statement_date or 'Unknown Period'
        report = _report_header(
            self.statement_type, self.client_name,
            account_number=self.account_number,
            statement_date=period, account_label='Account Ending'
        )

        total_all_deb = total_debits
        summary_rows = [
            ('Beginning Balance',        self.beginning_balance),
            ('Deposits and Credits',     total_deposits),
            ('Interest Earned',          self.interest_earned if self.interest_earned else None),
            ('Withdrawals and Debits',   total_all_deb),
            ('  Checks',                 total_checks if total_checks else None, 'indent'),
            ('  Payroll',                total_adp if total_adp else None, 'indent'),
            ('Ending Balance',           self.ending_balance),
        ]
        report += _summary_block(summary_rows)

        if self.beginning_balance is not None and self.ending_balance is not None:
            calc = self.beginning_balance + total_deposits + self.interest_earned - total_all_deb
            ok = abs(calc - self.ending_balance) < Decimal('0.01')
            report += _balance_check(ok, calc)

        report += _deposits_section(deposits, total_deposits, title='CREDITS / DEPOSITS')
        report += _charges_section(withdrawals, total_withdrawals, title='WITHDRAWALS AND DEBITS')
        if checks:
            report += _checks_section(checks, total_checks)
        if adp:
            report += _adp_section(adp, total_adp)
        return report


