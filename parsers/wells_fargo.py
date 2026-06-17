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
from parsers.vendor_normalize import strip_client_suffixes
from parsers.report import *

class WellsFargoCreditCardParser(StatementParser):
    """
    Wells Fargo Signify Business Essential Credit Card.

    Statement layout (pdftotext -layout):
      - Summary block on page 1: 'Previous Balance', 'Payments', 'New Balance'
      - Transactions on page 3+:
          MM/DD  MM/DD  <ref>  <description>  [credit_amt]  [charge_amt]
    """

    statement_type = "Wells Fargo Signify Business Essential Credit Card"

    def __init__(self, pdf_path, client_name=None):
        super().__init__(pdf_path, client_name)
        self.previous_balance = None
        self.new_balance = None
        self.payments = []
        self.credits = []
        self.charges = []
        self.finance_charge = Decimal('0')
        self.statement_year = None

    def _detect_client(self):
        return super()._detect_client()

    def parse(self):
        lines = self.text.split('\n')

        # Detect statement closing year from "Statement Closing Date MM/DD/YY"
        self.closing_date = None
        for line in lines:
            m = re.search(r'Statement Closing Date[\s.]+(\d{2}/\d{2}/(\d{2,4}))', line, re.IGNORECASE)
            if m:
                yr = m.group(2)
                self.statement_year = int(yr) if len(yr) == 4 else 2000 + int(yr)
                self.closing_date = m.group(1)
                break
        if self.statement_year is None:
            self.statement_year = 2025  # fallback

        # Parse summary balances
        for line in lines:
            if self.previous_balance is None:
                m = re.search(r'Previous Balance\s+\$?([\d,]+\.\d{2})', line)
                if m:
                    self.previous_balance = Decimal(m.group(1).replace(',', ''))
            if self.new_balance is None:
                m = re.search(r'New Balance\s*=?\s*\$?([\d,]+\.\d{2})', line)
                if m:
                    self.new_balance = Decimal(m.group(1).replace(',', ''))

        # Parse transactions using column position to distinguish Credits vs Charges columns.
        # Dynamically detect the Credits column position from the header line, since
        # the layout width varies between statement periods.
        # Finance charge appears as: PERIODIC *FINANCE CHARGE*  ...  <amount>
        CREDIT_COL_THRESHOLD = 116  # default fallback
        for line in lines:
            if 'Credits' in line and 'Charges' in line and 'Trans' in line:
                credits_pos = line.find('Credits')
                charges_pos = line.find('Charges')
                if credits_pos > 0 and charges_pos > credits_pos:
                    # Threshold = Credits column position + 15 chars
                    # (enough to cover the amount in the Credits column but
                    # well below where Charges-column amounts end)
                    CREDIT_COL_THRESHOLD = credits_pos + 15
                break

        in_transactions = False
        for line in lines:
            if 'Transaction Details' in line or ('Trans' in line and 'Post' in line and 'Description' in line):
                in_transactions = True
                continue
            if not in_transactions:
                continue

            # Finance charge line (no date prefix)
            fc_m = re.search(r'PERIODIC \*FINANCE CHARGE\*.*?([\d,]+\.\d{2})\s*$', line)
            if fc_m:
                self.finance_charge = Decimal(fc_m.group(1).replace(',', ''))
                continue

            # Transaction line: MM/DD  MM/DD  [REF]  DESCRIPTION  amount
            # Some lines (late charge, cash back) have no reference number
            m = re.match(
                r'(\d{2}/\d{2})\s+\d{2}/\d{2}\s+\S+\s+(.+?)\s+([\d,]+\.\d{2})\s*$',
                line
            )
            if not m:
                # Try without ref number: MM/DD  MM/DD  DESCRIPTION  amount
                m = re.match(
                    r'(\d{2}/\d{2})\s+\d{2}/\d{2}\s+(.+?)\s+([\d,]+\.\d{2})\s*$',
                    line
                )
            if not m:
                continue

            raw_date = m.group(1)
            raw_desc = m.group(2).strip()
            amt      = Decimal(m.group(3).replace(',', ''))

            month     = int(raw_date.split('/')[0])
            # If statement closes in Jan/Feb, December txns belong to prior year
            # Otherwise all txns belong to statement_year
            closing_month = int(self.closing_date.split('/')[0]) if self.closing_date else 1
            yr = (self.statement_year - 1) if (month > closing_month and month >= 11) else self.statement_year
            full_date = f"{raw_date}/{str(yr)[-2:]}"

            # Classify by column position (raw line length before strip)
            is_credit_col = len(line.rstrip('\n')) <= CREDIT_COL_THRESHOLD

            if is_credit_col:
                if re.search(r'ONLINE PAYMENT|PAYMENT THANK YOU', raw_desc, re.IGNORECASE):
                    self.payments.append({
                        'date': full_date,
                        'description': 'PAYMENT - THANK YOU',
                        'amount': amt,
                    })
                else:
                    # Return / credit — normalize description same as charges
                    self.credits.append({
                        'date': full_date,
                        'description': self.normalize_vendor(raw_desc),
                        'amount': amt,
                    })
            else:
                self.charges.append({'date': full_date, 'vendor': raw_desc, 'amount': amt})

    def generate_report(self):
        aggregated = self._aggregate_by_vendor(self.charges, date_fmt='%m/%d/%y')
        total_charges  = sum(r['amount'] for r in aggregated)
        total_payments = sum(p['amount'] for p in self.payments)
        total_credits  = sum(c['amount'] for c in self.credits)

        # Add MISSING row if totals don't tie
        # new_bal = prev - payments - credits + charges + finance_charge
        # => expected_charges = new_bal - prev + payments + credits - finance_charge
        statement_charges = None
        if self.new_balance is not None and self.previous_balance is not None:
            statement_charges = (self.new_balance - self.previous_balance
                                 + total_payments + total_credits
                                 - self.finance_charge)
        aggregated, total_charges = _add_missing_row(aggregated, total_charges, statement_charges)

        report = _report_header(self.statement_type, self.client_name,
                                     statement_date=self.closing_date)

        summary_rows = [
            ('Previous Balance',  self.previous_balance),
            ('Payments',          total_payments),
            ('Credits / Returns', total_credits if total_credits else None),
            ('Purchases',       total_charges),
            ('Finance Charges',    self.finance_charge if self.finance_charge else None),
            ('New Balance',       self.new_balance),
        ]
        report += _summary_block(summary_rows)

        if self.previous_balance is not None and self.new_balance is not None:
            calc = self.previous_balance - total_payments - total_credits + total_charges + self.finance_charge
            ok   = abs(calc - self.new_balance) < Decimal('0.01')
            report += _balance_check(ok, calc)

        if self.payments:
            report += _payments_section(self.payments, total_payments)
        if self.credits:
            report += _credits_section(self.credits, total_credits)
        report += _charges_section(aggregated, total_charges)
        return report

    def normalize_vendor(self, description):
        return _registry.normalize_vendor(self.client_name or '', description)



class WellsFargoCheckingParser(StatementParser):
    """
    Wells Fargo Initiate Business Checking.

    Statement layout (pdftotext -layout):
      - Summary block: 'Beginning balance on M/D', 'Deposits/Credits',
        'Withdrawals/Debits', 'Ending balance on M/D'
      - Transactions: date at col ~7, optional '<' at col ~25 (B2B ACH),
        description, then amount in Deposits/Credits col or Withdrawals/Debits col
        determined by horizontal position.
      - Continuation lines (no date) are part of prior transaction description.
    """

    statement_type = "Wells Fargo Initiate Business Checking"

    def __init__(self, pdf_path, client_name=None):
        super().__init__(pdf_path, client_name)
        self.beginning_balance = None
        self.ending_balance    = None
        self.statement_period  = None
        self.credits    = []
        self.debits     = []
        self.checks     = []
        self.bank_fees  = []

    def _detect_client(self):
        return super()._detect_client()

    def _normalize(self, desc):
        d = desc.strip()
        # Strip long reference codes: sequences of 10+ alphanum chars
        d = re.sub(r'\s+[A-Z0-9]{10,}\s*', ' ', d).strip()
        # Strip trailing location/cardholder noise (config-driven via
        # description_strip_suffixes), using the shared normalization helper.
        cfg = _registry.get_config(self.client_name or '') or {}
        d = strip_client_suffixes(d, cfg.get('description_strip_suffixes'))
        # Client-specific vendor naming (e.g. transfers to named individuals)
        # comes from the client config's vendor_rules — no client names here.
        configured = _registry.normalize_vendor(self.client_name or '', d)
        if configured != d:
            return configured
        # Normalize generic vendors
        u = d.upper()
        if 'SQUARE INC SQ' in u or (u.startswith('SQUARE INC') and 'PAYR' not in u and 'FIN' not in u):
            return 'Square Inc'
        if 'SQUARE INC PAYR TAX' in u:
            return 'Square Inc Payr Tax'
        if 'SQUARE INC PAYR DD' in u:
            return 'Square Inc Payr DD'
        if 'SQUARE FIN SVCS' in u:
            return 'Square Fin Svcs Transfer'
        if 'IRS USATAXPYMT' in u or 'IRS USATAX' in u:
            return 'IRS Usataxpymt'
        if 'EMPLOYMENT DEVEL EDD' in u or 'EDD EFTPMT' in u:
            return 'Employment Devel EDD'
        if 'NEXT INSUR' in u:
            return 'Next Insur'
        if 'ONLINE TRANSFER' in u and 'WAY2SAVE' in u and 'FROM' in u:
            return 'Transfer From Way2Save Savings'
        if 'ONLINE TRANSFER' in u and 'WAY2SAVE' in u:
            return 'Transfer to Way2Save Savings'
        if 'ONLINE TRANSFER' in u:
            return 'Online Transfer'
        if 'ZELLE TO' in u:
            m = re.search(r'Zelle to ([A-Za-z\s]+?)\s+on', d, re.IGNORECASE)
            return f"Zelle to {m.group(1).strip()}" if m else 'Zelle Payment'
        if 'ZELLE FROM' in u:
            m = re.search(r'Zelle from ([A-Za-z\s]+?)\s+on', d, re.IGNORECASE)
            return f"Zelle From {m.group(1).strip()}" if m else 'Zelle Receipt'
        if 'CHECK REFERENCE' in u or 'CHECK REF' in u:
            return 'Check Returned Unpaid'
        if 'OVERDRAFT FEE' in u:
            chk = re.search(r'CHECK\s*#\s*0*(\d+)', u)
            return f"Overdraft Fee - Check #{chk.group(1)}" if chk else 'Overdraft Fee'
        return d

    def parse(self):
        lines = self.text.split('\n')

        # Extract statement period and balances
        for line in lines:
            m = re.search(r'Beginning balance on (\d+/\d+)\s+\$?([\d,]+\.\d{2})', line)
            if m:
                self.beginning_balance = Decimal(m.group(2).replace(',', ''))
            m = re.search(r'Ending balance on (\d+/\d+)\s+\$?([\d,]+\.\d{2})', line)
            if m:
                self.ending_balance = Decimal(m.group(2).replace(',', ''))
            m = re.search(r'(\w+ \d+, \d{4})\s+Page', line)
            if m and not self.statement_period:
                self.statement_period = m.group(1)

        # Column positions are detected dynamically per-section header, since
        # Wells Fargo uses different column widths on different pages of the same statement.
        dep_col = 95   # fallback
        deb_col = 112  # fallback

        # Pre-scan ALL header lines and store their positions so we can update
        # dep_col/deb_col as we encounter each new section header during parsing.
        header_cols = {}  # line_index -> (dep_col, deb_col)
        for idx, line in enumerate(lines):
            if 'Deposits/' in line and 'Withdrawals/' in line:
                dc = line.find('Deposits/')
                wc = line.find('Withdrawals/')
                if dc > 0 and wc > dc:
                    header_cols[idx] = (dc, wc)

        # Parse transactions
        # Lines starting with a date pattern (M/D or MM/DD) are transaction lines
        # Continuation lines (indented, no date) extend the prior description
        in_transactions = False
        current = None  # {'date','desc','col_dep','col_deb','raw_line'}

        def flush(txn):
            if not txn:
                return
            desc = self._normalize(txn['desc'])
            amt_str = txn['amt_str'].replace(',', '')
            try:
                amt = Decimal(amt_str)
            except Exception:
                return
            date = txn['date']
            if txn['is_credit']:
                self.credits.append({'date': date, 'vendor': desc, 'amount': amt})
            else:
                # Check line?
                chk = txn.get('check_num', '')
                if chk:
                    # Clean payee: strip header bleed and generic "Check" label
                    payee = re.sub(r'\s*Deposits/.*$', '', desc, flags=re.IGNORECASE).strip()
                    payee = re.sub(r'^\s*Check\s*$', '', payee, flags=re.IGNORECASE).strip()
                    payee = re.sub(r'\s+Check\s*$', '', payee, flags=re.IGNORECASE).strip()
                    self.checks.append({'date': date, 'check_num': chk,
                                        'payee': payee, 'amount': amt})
                elif 'WIRE TRANS SVC CHARGE' in desc.upper() or 'OVERDRAFT FEE' in desc.upper():
                    self.bank_fees.append({'date': date, 'vendor': desc, 'amount': amt})
                else:
                    self.debits.append({'date': date, 'vendor': desc, 'amount': amt})

        for line_idx, line in enumerate(lines):
            # Detect start of transaction section
            if 'Transaction history' in line or 'Transaction History' in line:
                in_transactions = True
                continue
            if 'Totals' in line and in_transactions:
                flush(current)
                current = None
                in_transactions = False
                continue
            if not in_transactions:
                continue

            # Update column positions when a new section header is encountered
            if line_idx in header_cols:
                dep_col, deb_col = header_cols[line_idx]

            # Skip header lines
            if re.search(r'Deposits/\s*Credits|Withdrawals/\s*Debits|Date\s+Check', line):
                continue

            # Date line: starts with spaces then M/D or MM/DD
            date_m = re.match(r'^\s{3,8}(\d{1,2}/\d{1,2})\s+', line)
            if date_m:
                flush(current)
                current = None
                raw_date = date_m.group(1)
                # Zero-pad to MM/DD for correct chronological sorting
                parts = raw_date.split('/')
                raw_date = f"{int(parts[0]):02d}/{int(parts[1]):02d}"
                # Check for check number (digits between date and description)
                rest = line[date_m.end():]
                check_m = re.match(r'(\d{4,6})\s+', rest)
                check_num = ''
                if check_m:
                    check_num = check_m.group(1)
                    rest = rest[check_m.end():]

                # Find amount: look for rightmost number pattern
                # Determine if it's a deposit or debit by column position
                amt_m = re.search(r'([\d,]+\.\d{2})\s*(?:[\d,]+\.\d{2}\s*)?$', line)
                # Find all amounts on this line
                amounts = [(m.start(), m.group()) for m in re.finditer(r'[\d,]+\.\d{2}', line)]
                if not amounts:
                    # No amount yet — continuation will add it
                    desc = rest.strip().rstrip('\n')
                    current = {'date': raw_date, 'desc': desc, 'amt_str': '',
                               'is_credit': False, 'check_num': check_num}
                    continue

                # Use column position to determine credit vs debit and pick the
                # correct transaction amount.
                #
                # Wells Fargo -layout columns:
                #   dep_col  ≈ 111  (Deposits/Credits column header)
                #   deb_col  ≈ 128  (Withdrawals/Debits column header)
                #   ending balance column is further right (~145+)
                #
                # Amounts that appear *before* dep_col are part of the description
                # text (e.g. "5.15" in "Ref # Bacxfcnsd2Q5 5.15") and must be
                # ignored.  The real transaction amount sits at dep_col or deb_col;
                # ending-balance amounts are past deb_col+20 and are also excluded.
                financial_amts = [(pos, val) for pos, val in amounts
                                  if dep_col - 10 <= pos < deb_col + 20]
                if not financial_amts:
                    # Fallback: last amount before the balance column
                    financial_amts = [(pos, val) for pos, val in amounts
                                      if pos < deb_col + 20]
                if not financial_amts:
                    financial_amts = amounts[:1]

                # The transaction amount is whichever financial amount is closest
                # to dep_col (credits) or deb_col (debits).  Use the one with the
                # smallest distance to either column header.
                txn_pos, txn_val = min(
                    financial_amts,
                    key=lambda pv: min(abs(pv[0] - dep_col), abs(pv[0] - deb_col))
                )
                # Credit if the amount column is closer to dep_col than deb_col
                is_credit = abs(txn_pos - dep_col) <= abs(txn_pos - deb_col)

                # Description is everything between date+check and the first
                # *financial* amount (amounts purely in the description are skipped)
                desc_end = txn_pos
                # If there are pre-column amounts in the description, trim desc to
                # stop before the first one so we don't include "5.15" etc.
                pre_desc_amts = [(pos, val) for pos, val in amounts if pos < dep_col - 10]
                if pre_desc_amts:
                    desc_end = pre_desc_amts[0][0]
                desc = line[date_m.end() + (len(check_num) + 1 if check_num else 0):desc_end].strip()
                # Remove leading '<' (B2B ACH marker)
                desc = re.sub(r'^<\s*', '', desc).strip()
                # For checks, normalize the payee — strip any table-header bleed or trailing noise
                if check_num:
                    desc = re.sub(r'\s+Deposits/.*$', '', desc, flags=re.IGNORECASE).strip()
                    desc = re.sub(r'^\s*Check\s*$', '', desc, flags=re.IGNORECASE).strip()
                    desc = re.sub(r'\s+Check\s*$', '', desc, flags=re.IGNORECASE).strip()
                    if not desc:
                        desc = ''  # leave blank; _checks_section handles missing payee

                current = {'date': raw_date, 'desc': desc,
                           'amt_str': txn_val.replace(',', ''),
                           'is_credit': is_credit, 'check_num': check_num}
                # Checks are always debits regardless of column detection
                if check_num:
                    current['is_credit'] = False
            elif current is not None and re.match(r'^\s{10,}', line) and line.strip():
                # Continuation line — may contain the amount or extend description
                amounts = [(m.start(), m.group()) for m in re.finditer(r'[\d,]+\.\d{2}', line)]
                financial_amts = [(pos, val) for pos, val in amounts if pos < deb_col + 20]
                if financial_amts and not current['amt_str']:
                    pos, val = financial_amts[0]
                    current['is_credit'] = pos < (dep_col + deb_col) // 2
                    current['amt_str'] = val.replace(',', '')
                elif line.strip() and not re.search(r'[\d,]+\.\d{2}', line):
                    # Pure description continuation
                    current['desc'] += ' ' + line.strip()

        flush(current)

        # (Items returned unpaid are already captured in main transaction loop)

    def _extract_period(self):
        return self.statement_period or 'Unknown Period'

    def generate_report(self, check_payee_map=None, check_date_map=None):
        # Aggregate deposits and debits by vendor
        def agg(txns, date_key='vendor'):
            from collections import defaultdict
            totals = defaultdict(lambda: {'amount': Decimal('0'), 'count': 0, 'date': ''})
            for t in txns:
                v = t[date_key]
                totals[v]['amount'] += t['amount']
                totals[v]['count']  += 1
                totals[v]['date']    = t['date']
            result = []
            for vendor, d in totals.items():
                result.append({'date': d['date'], 'vendor': vendor,
                                'amount': d['amount'], 'count': d['count']})
            result.sort(key=lambda x: x['date'])
            return result

        # Square Payroll section (individual lines): payroll tax, direct deposit, EDD, IRS
        # Everything else: Withdrawals and Debits, aggregated
        PAYROLL_VENDORS = {'Square Inc Payr Tax', 'Square Inc Payr DD', 'Employment Devel EDD',
                           'IRS Usataxpymt'}
        IRS_VENDORS     = set()  # IRS now grouped under payroll

        cc_payments = [d for d in self.debits if 'WFB Credit Card' in d['vendor'] or 'Online Transfer to' in d['vendor']]
        payroll     = [d for d in self.debits if d['vendor'] in PAYROLL_VENDORS]
        irs_deb     = []
        other_deb   = [d for d in self.debits if d not in cc_payments and d not in payroll]

        agg_dep  = agg(self.credits)
        agg_deb  = agg(other_deb)
        agg_cc   = agg(cc_payments)
        agg_fees = agg(self.bank_fees)
        # Payroll (incl. IRS): NOT aggregated — show each transaction individually
        pay_rows = sorted(payroll, key=lambda x: x['date'])
        irs_rows = []

        total_dep  = sum(r['amount'] for r in agg_dep)
        total_deb  = sum(r['amount'] for r in agg_deb)
        total_pay  = sum(r['amount'] for r in pay_rows)
        total_cc   = sum(r['amount'] for r in agg_cc)
        total_chk  = sum(c['amount'] for c in self.checks)
        total_fees = sum(r['amount'] for r in agg_fees)
        total_all_deb = total_deb + total_pay + total_cc + total_chk + total_fees

        report = _report_header(self.statement_type, self.client_name,
                                statement_date=self._extract_period())

        summary_rows = [
            ('Beginning Balance',          self.beginning_balance),
            ('Deposits and Credits',        total_dep),
            ('Withdrawals and Debits',      total_all_deb),
            ('  Checks',                    total_chk if total_chk else None, 'indent'),
            ('  Payroll',                   total_pay if total_pay else None, 'indent'),
            ('  Credit Card Payments',      total_cc if total_cc else None, 'indent'),
            ('  Bank Fees',                 total_fees if total_fees else None, 'indent'),
            ('Ending Balance',              self.ending_balance),
        ]
        report += _summary_block(summary_rows)

        if self.beginning_balance is not None and self.ending_balance is not None:
            calc = (self.beginning_balance + total_dep - total_all_deb)
            ok   = abs(calc - self.ending_balance) < Decimal('0.01')
            report += _balance_check(ok, calc)

        def _individual_section(rows, total, title):
            W = 80
            lines_s = ['=' * W, title, '',
                       f"{'Date':<12} {'Description':<50} {'Amount':>16}",
                       '-' * W]
            for r in rows:
                lines_s.append(f"{r['date']:<12} {r['vendor']:<50} ${r['amount']:>15,.2f}")
            lines_s.append(f"{'':63} {'-' * 17}")
            lines_s.append(f"{'TOTAL ' + title + ':':<63} ${total:>15,.2f}")
            lines_s.append('')
            return '\n'.join(lines_s) + '\n'

        report += _deposits_section(agg_dep, total_dep)

        # Withdrawals: aggregated other debits + individual IRS lines
        if agg_deb or irs_rows:
            W = 80
            irs_total = sum(r['amount'] for r in irs_rows)
            other_total = sum(r['amount'] for r in agg_deb)
            lines_wd = ['=' * W, 'WITHDRAWALS AND DEBITS', '',
                        f"{'Date':<12} {'Description':<50} {'Amount':>16}",
                        '-' * W]
            for r in sorted(irs_rows, key=lambda x: x['date']):
                lines_wd.append(f"{r['date']:<12} {r['vendor']:<50} ${r['amount']:>15,.2f}")
            for r in agg_deb:
                count_str = f" ({r['count']})" if r['count'] > 1 else ''
                lines_wd.append(f"{r['date']:<12} {(r['vendor']+count_str):<50} ${r['amount']:>15,.2f}")
            lines_wd.append(f"{'':63} {'-' * 17}")
            lines_wd.append(f"{'TOTAL WITHDRAWALS AND DEBITS:':<63} ${total_deb:>15,.2f}")
            lines_wd.append('')
            report += '\n'.join(lines_wd) + '\n'

        if self.checks:
            report += _checks_section(self.checks, total_chk)
        if pay_rows:
            report += _individual_section(pay_rows, total_pay, 'PAYROLL')
        if agg_cc:
            cc_rows = sorted(cc_payments, key=lambda x: x['date'])
            report += _individual_section(cc_rows, total_cc, 'CREDIT CARD PAYMENTS')
        if agg_fees:
            pass  # Bank Fees shown in summary only
        return report

    def normalize_vendor(self, description):
        return self._normalize(description)

