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

from parsers.base import StatementParser, _registry, KNOWN_CLIENTS, CLIENT_CANONICAL
from parsers.report import *
from parsers.report import (
    _report_header, _summary_block, _balance_check,
    _payments_section, _credits_section, _individual_section, _deposits_section,
    _checks_section, _adp_section, _cc_payments_section, _add_missing_row,
    _charges_section, _safe_date_key, _now_pst
)

class CitiCheckingParser(StatementParser):
    """
    Citi Business Checking.

    Statement format (multi-line transactions):
      MM/DD  ACH DEBIT  AMOUNT  BALANCE
             VENDOR NAME  ...
    Special sections: ADP (never aggregate), Credit Card Payments (never aggregate).
    Client config may also specify `no_aggregate_vendors` (list of substrings);
    matching vendors are kept as individual line items rather than aggregated.
    """
    statement_type = "Citi Business Checking"

    def __init__(self, pdf_path, client_name=None):
        super().__init__(pdf_path, client_name)
        self.account_number = ''
        self.statement_date = ''
        self.closing_date = None
        self.previous_balance = Decimal('0')
        self.new_balance = Decimal('0')
        self.payments = []
        self.credits = []
        self.charges = []
        self.adp_transactions = []
        self.credit_card_payments = []
        self.checks = []
        self.total_payments = Decimal('0')
        self.total_credits = Decimal('0')
        self.total_charges = Decimal('0')
        self.total_checks = Decimal('0')

    def _detect_client(self):
        # Citi statements: check first 20 lines for known client names
        lines = self.text.split('\n')
        for line in lines[:20]:
            line_upper = line.upper()
            for name in KNOWN_CLIENTS:
                if name in line_upper:
                    return CLIENT_CANONICAL.get(name, name)
        return super()._detect_client()

    def parse(self):
        lines = self.text.split('\n')

        found_beginning = False
        found_ending = False
        for line in lines:
            if 'Beginning Balance:' in line and not found_beginning:
                m = re.match(r'(\d{9})', line.strip())
                if m:
                    self.account_number = m.group(1)
                bm = re.search(r'\$([\d,]+\.\d{2})', line)
                if bm:
                    self.previous_balance = Decimal(bm.group(1).replace(',', ''))
                    found_beginning = True
            if 'Ending Balance:' in line and not found_ending:
                bm = re.search(r'\$([\d,]+\.\d{2})', line)
                if bm:
                    self.new_balance = Decimal(bm.group(1).replace(',', ''))
                    found_ending = True
            if 'Statement Period' in line:
                m = re.search(r'(\w+ \d+ - \w+ \d+, \d{4})', line)
                if m:
                    self.statement_date = m.group(1)
                    # Extract closing year and month from "January 1 - January 31, 2026"
                    ym = re.search(r'(\w+)\s+\d+,\s+(\d{4})$', m.group(1))
                    if ym:
                        month_names = {'january':1,'february':2,'march':3,'april':4,
                                       'may':5,'june':6,'july':7,'august':8,
                                       'september':9,'october':10,'november':11,'december':12,
                                       'jan':1,'feb':2,'mar':3,'apr':4,
                                       'jun':6,'jul':7,'aug':8,
                                       'sep':9,'oct':10,'nov':11,'dec':12}
                        close_mm = month_names.get(ym.group(1).lower(), 1)
                        close_yy = str(int(ym.group(2)) % 100).zfill(2)
                        self.closing_date = f"{close_mm:02d}/28/{close_yy}"

        i = 0
        while i < len(lines):
            line = lines[i]
            dm = re.match(r'^(\d{2}/\d{2})\s+(.+)', line)
            if dm:
                date = self._add_year_to_date(dm.group(1), self.closing_date) if self.closing_date else dm.group(1)
                rest = dm.group(2)
                vendor_line = lines[i + 1] if i + 1 < len(lines) else ''

                trans_type = None
                for ttype in ['ACH DEBIT', 'ACH CREDIT', 'ELECTRONIC CREDIT',
                               'INSTANT PAYMENT CREDIT', 'DEPOSIT']:
                    if ttype in rest:
                        trans_type = ttype
                        break
                if 'CHECK NO:' in rest:
                    trans_type = 'CHECK'

                if not trans_type:
                    i += 1
                    continue

                amounts = re.findall(r'([\d,]+\.\d{2})', line)
                if len(amounts) < 2:
                    i += 1
                    continue

                amount = Decimal(amounts[-2].replace(',', ''))
                vendor = vendor_line.strip()

                if trans_type in ('ACH DEBIT', 'ACH CREDIT'):
                    vendor = re.split(r'\s{2,}', vendor)[0].strip()
                    if trans_type == 'ACH DEBIT':
                        if 'ADP' in vendor.upper():
                            self.adp_transactions.append(
                                {'date': date, 'vendor': vendor, 'amount': str(amount)})
                            self.total_charges += amount
                            i += 1
                            continue
                        no_agg = (_registry.get_config(self.client_name) or {}).get('no_aggregate_vendors', [])
                        if any(v.upper() in vendor.upper() for v in no_agg):
                            # Tag with date to prevent aggregation; display strips the tag.
                            vendor = f'{vendor}|{date}'
                        if 'CREDIT CRD' in vendor.upper() or 'AUTOPAY' in vendor.upper():
                            self.credit_card_payments.append(
                                {'date': date, 'vendor': vendor, 'amount': str(amount)})
                            self.total_charges += amount
                            i += 1
                            continue
                elif trans_type == 'INSTANT PAYMENT CREDIT':
                    vendor = 'Merchant Deposits'
                elif trans_type == 'DEPOSIT':
                    vendor = 'Deposit'
                elif trans_type == 'CHECK':
                    check_num_m = re.search(r'CHECK NO:\s*(\d+)', rest)
                    check_num = check_num_m.group(1) if check_num_m else '?'
                    self.checks.append({'date': date, 'check_num': check_num, 'amount': amount})
                    self.total_checks += amount
                    i += 1
                    continue

                if trans_type in ('INSTANT PAYMENT CREDIT', 'DEPOSIT', 'ACH CREDIT', 'ELECTRONIC CREDIT'):
                    self.credits.append({'date': date, 'vendor': vendor, 'amount': amount})
                    self.total_credits += amount
                else:
                    self.charges.append({'date': date, 'vendor': vendor, 'amount': str(amount)})
                    self.total_charges += amount

            i += 1

    def generate_report(self, check_payee_map=None, check_date_map=None):
        check_payee_map = check_payee_map or {}
        for ck in self.checks:
            num = str(ck.get('check_num', ''))
            if num in check_payee_map:
                ck['payee'] = check_payee_map[num]

        aggregated = self._aggregate_by_vendor(self.charges, date_fmt='%m/%d/%y')
        total_charges = sum(r['amount'] for r in aggregated)
        total_credits = sum(c['amount'] for c in self.credits)
        total_adp = sum(Decimal(str(a['amount'])) for a in self.adp_transactions)
        total_cc = sum(Decimal(str(c['amount'])) for c in self.credit_card_payments)
        total_checks = sum(Decimal(str(c['amount'])) for c in self.checks)

        aggregated_credits = self._aggregate_by_vendor(self.credits, date_fmt='%m/%d/%y')
        total_all_deb = total_charges + total_adp + total_cc + total_checks

        report = _report_header(self.statement_type, self.client_name,
                                account_number=self.account_number,
                                statement_date=self.statement_date)
        report += _summary_block([
            ('Beginning Balance',        self.previous_balance),
            ('Deposits and Credits',     total_credits),
            ('Withdrawals and Debits',   total_all_deb),
            ('  Checks',                 total_checks if total_checks else None, 'indent'),
            ('  Payroll',                total_adp if total_adp else None, 'indent'),
            ('  Credit Card Payments',   total_cc if total_cc else None, 'indent'),
            ('Ending Balance',           self.new_balance),
        ])

        calc = self.previous_balance + total_credits - total_all_deb
        ok = abs(calc - self.new_balance) < Decimal('0.01')
        report += _balance_check(ok, calc)

        if self.credits:
            report += _deposits_section(aggregated_credits, total_credits,
                                        title='UNDEPOSITED SALES RECEIPTS',
                                        account_label='UNDEPOSITED SALES RECEIPTS')
        report += _charges_section(aggregated, total_charges, title='WITHDRAWALS AND DEBITS')
        if self.checks:
            report += _checks_section(self.checks, total_checks)
        if self.adp_transactions:
            report += _adp_section(self.adp_transactions, total_adp)
        if self.credit_card_payments:
            report += _cc_payments_section(self.credit_card_payments, total_cc)
        return report


class CitiVisaCostcoParser(StatementParser):
    """
    Citi Costco Anywhere Visa Credit Card.

    Statement format:
      - Transactions: MM/DD  DESCRIPTION  AMOUNT  (charges are positive)
      - Payments:     MM/DD  PAYMENT / CREDIT  AMOUNT  (shown as negative or in credits section)
      - Balances: 'Previous Balance', 'New Balance' lines
      - Reference numbers after descriptions are stripped during parsing
    """
    statement_type = "Citi Costco Anywhere Visa Credit Card"

    def __init__(self, pdf_path, client_name=None):
        super().__init__(pdf_path, client_name)
        self.previous_balance = Decimal('0')
        self.new_balance = Decimal('0')
        self.total_payments = Decimal('0')
        self.finance_charge = Decimal('0')
        self.payments = []
        self.credits = []
        self.charges = []
        self.closing_date = None  # MM/DD/YY format

    def parse(self):
        raw_lines = self.text.split('\n')

        # ── Extract billing period / closing date ────────────────────────────
        for line in raw_lines:
            # "Billing Period: 12/19/25-01/20/26"
            m = re.search(r'Billing Period.*?(\d{2}/\d{2}/\d{2})\s*-\s*(\d{2}/\d{2}/\d{2})', line)
            if m and not self.closing_date:
                self.closing_date = m.group(2)  # closing date is the later one
                break

        # ── Balances ─────────────────────────────────────────────────────────
        self.statement_new_charges = Decimal('0')
        for line in raw_lines:
            if re.search(r'Previous [Bb]alance', line) and self.previous_balance == Decimal('0'):
                m = re.search(r'-?\$?\s*([\d,]+\.\d{2})', line)
                if m:
                    self.previous_balance = abs(Decimal(m.group(1).replace(',', '')))
            # "New balance as of MM/DD/YY: $XXX" or "New Balance  $XXX"
            if re.search(r'New [Bb]alance', line) and self.new_balance == Decimal('0'):
                if not re.search(r'Minimum|minimum', line):
                    m = re.search(r'\$?\s*([\d,]+\.\d{2})', line)
                    if m:
                        self.new_balance = Decimal(m.group(1).replace(',', ''))
            # New balance on standalone amount line (e.g. "                  $396.11")
            # Only pick up if we haven't found it yet and the line is ONLY a dollar amount
            if self.new_balance == Decimal('0'):
                standalone = re.match(r'^\s+\$([\d,]+\.\d{2})\s*$', line)
                if standalone:
                    # Only use if previous line contained "New" or "balance"
                    idx = raw_lines.index(line)
                    prev_line = raw_lines[idx - 1] if idx > 0 else ''
                    if re.search(r'[Nn]ew|[Bb]alance', prev_line):
                        self.new_balance = Decimal(standalone.group(1).replace(',', ''))
            # Sum all "New Charges" lines to get statement total
            if re.search(r'New Charges', line):
                m = re.search(r'\$\s*([\d,]+\.\d{2})', line)
                if m:
                    self.statement_new_charges += Decimal(m.group(1).replace(',', ''))
            # Interest / Finance Charge
            if re.search(r'Interest Charged|Finance Charge', line, re.IGNORECASE) and not self.finance_charge:
                m = re.search(r'\$?\s*([\d,]+\.\d{2})', line)
                if m:
                    self.finance_charge = Decimal(m.group(1).replace(',', ''))

        # ── Pre-process: fix common OCR issues ────────────────────────────────
        def fix_ocr_line(line):
            # Strip sidebar rewards: $X.XX followed by 6+ spaces then another $Y.YY at end
            line = re.sub(r'(\$[\d,]+\.\d{2})\s{6,}.*?[+]?\$[\d,]+\.\d{2}\s*$', r'\1', line)
            # Strip trailing non-numeric junk after spaced cents (e.g. '$131 42 y')
            line = re.sub(r'(\$\d+\s\d{2})\s+[A-Za-z~]+\s*$', r'\1', line)
            # Fix OCR ^ prefix: '^MAZON' -> 'AMAZON'
            line = re.sub(r'\^MAZON', 'AMAZON', line)
            # Strip leading noise characters before dates: 'f01/14' -> '01/14', 'a01/20' -> '01/20'
            line = re.sub(r'(?<!\d)[a-z](\d{2}/\d{2})', r' \1', line)
            # Fix spaced-out single-letter words: 'T M O B I L E' -> 'TMOBILE' (7, 6, 5 letters)
            line = re.sub(r'\b([A-Z])\s([A-Z])\s([A-Z])\s([A-Z])\s([A-Z])\s([A-Z])\s([A-Z])\b', r'\1\2\3\4\5\6\7', line)
            line = re.sub(r'\b([A-Z])\s([A-Z])\s([A-Z])\s([A-Z])\s([A-Z])\s([A-Z])\b', r'\1\2\3\4\5\6', line)
            line = re.sub(r'\b([A-Z])\s([A-Z])\s([A-Z])\s([A-Z])\s([A-Z])\b', r'\1\2\3\4\5', line)
            # Fix spaced dates like '1 2 / 2 3' -> '12/23'
            line = re.sub(r'\b(\d)\s+(\d)\s*/\s*(\d)\s+(\d)\b', r'\1\2/\3\4', line)
            # Collapse spaces around slash in date context
            line = re.sub(r'(\d{2})\s*/\s*(\d{2})', r'\1/\2', line)
            # Fix OCR letter subs in date-like tokens: O->0, i/l/I->1
            def fix_date_token(m):
                s = m.group(0)
                s = re.sub(r'[Oo]', '0', s)
                s = re.sub(r'[ilI]', '1', s)
                s = re.sub(r'[sS]', '5', s)
                return s
            line = re.sub(r'(?<![/\d])([Ooil\dsS]{2})/([Ooil\dsS]{2})(?![/\d])', fix_date_token, line)
            # Remove oiToi-style OCR noise tokens (not parseable as date)
            line = re.sub(r'(?<![/\d])([Ooil\dsS]{2})[A-Za-z]+([Ooil\dsS]{2})(?![/\d])\s*', '', line)
            # Fix concatenated dates: '01/13Oi7i3' -> '01/13'
            line = re.sub(r'(\d{2}/\d{2})[A-Za-z\d]{2,}(?=\s)', r'\1', line)
            # Fix leading digit+underscore OCR noise before vendor: '0_SALONCENTRIC' -> 'SALONCENTRIC'
            line = re.sub(r'\b\d[_]\s*', '', line)
            # Fix broken date suffix: '12/2T~' -> '12/2'
            line = re.sub(r'(\d{2}/\d)[T~?]+', r'\1', line)
            # Strip unicode/curly quotes
            for ch in ['\u201c', '\u201d', '\u2018', '\u2019', '\u201c', '\u201d']:
                line = line.replace(ch, '')
            # Fix spaced cents: '$131 42' -> '$131.42'
            line = re.sub(r'(\$\d+)\s(\d{2})\s*$', r'\1.\2', line)
            # Add missing $ on bare decimal at end: '  147.00' -> '  $147.00'
            line = re.sub(r'(\s)(\d{1,5}\.\d{2})\s*$', r'\1$\2', line)
            return line

        lines = [fix_ocr_line(l) for l in raw_lines]

        # ── Transaction parsing ───────────────────────────────────────────────
        # Strategy: find the transaction amount as the LAST dollar-amount that is
        # either followed by 3+ spaces (sidebar column gap) or end of line.
        # This handles the cardholder summary page where the rewards sidebar
        # bleeds onto the same line after a wide gap.

        skip_keywords = [
            'BILLING PERIOD', 'BILLING INQUIRIES', 'MINIMUM PAYMENT', 'PAYMENT DUE',
            'CREDIT LIMIT', 'AVAILABLE CREDIT', 'COSTCO CASH BACK', 'REWARDS SUMMARY',
            'REWARDS BALANCE', 'EARNED THIS PERIOD', 'TOTAL EARNED', 'YEAR TO DATE',
            'AUTOPAY', 'DEDUCTED FROM', 'INTEREST CHARGE', 'DAYS IN BILLING',
            'ACCOUNT MESSAGES', 'ANNUAL PERCENTAGE', 'BALANCE SUBJECT',
            'FEES FOR THIS PERIOD', 'INTEREST FOR THIS PERIOD',
            '5% ON GAS', '4% ON', '3% ON', '2% ON COSTCO', '1% ON ALL',
            'SEE PAGE', 'VISIT CITI', 'CARDHOLDER SUMMARY',
            'PAYMENTS, CREDITS', 'ACCOUNT SUMMARY', 'STANDARD PURCHASES',
            'FEES CHARGED', 'INTEREST CHARGED', 'TOTALS YEAR',
            'PREVIOUS BALANCE', 'BALANCE TYPE', 'NEW CHARGES',
        ]

        def clean_vendor(v):
            v = re.sub(r'\s+\d{10,}\s*$', '', v)
            v = re.sub(r'\s+\d{3}-\d{3}-\d{4}\s*[A-Z]{0,2}\s*$', '', v)
            v = re.sub(r'\s+[A-Z]{2}\s*$', '', v)
            v = re.sub(r'\s+HTTP[S]?://\S+', '', v, flags=re.IGNORECASE)
            v = re.sub(r'\s*[,;]\s*$', '', v)
            return re.sub(r'\s+', ' ', v).strip()

        # Find amount: $X.XX followed by 3+ spaces (sidebar gap) OR end of line
        p_amt = re.compile(r'([-]?\$[\d,]+\.\d{2})(?:\s{3,}|\s*$)')

        # Date pattern: MM/DD but NOT MM/DD/YY (exclude full dates like 01/20/26)
        DATE_PAT = r'\d{2}/\d{2}(?!/\d{2})'

        pending_date   = None
        pending_vendor = None

        for idx, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue  # blank line — keep pending state, amount may follow
            upper = stripped.upper()
            # Only skip if the keyword appears in the first half of the line
            # (sidebar text after the transaction amount should not trigger a skip)
            check_portion = stripped[:len(stripped)//2 + 20].upper()
            if any(kw in check_portion for kw in skip_keywords):
                pending_date = pending_vendor = None
                continue

            # Check if line is just an amount — continuation of previous
            amt_only = re.match(r'^\s*[-]?\$?([\d,]+\.\d{2})[,\s]*$', line)
            if amt_only and pending_date and pending_vendor:
                try:
                    amount = Decimal(amt_only.group(1).replace(',', ''))
                    self._store_transaction(pending_date, pending_vendor, amount)
                except Exception:
                    pass
                pending_date = pending_vendor = None
                continue

            # Check if line is a vendor+amount continuation (no date, has $ amount)
            # e.g. '    AMAZON MKTPL+JR0MG90Q3 Amzn.com/billWA   $142.18'
            if pending_date and not re.search(r'\d{2}/\d{2}', line):
                cont_m = re.search(r'([-]?\$[\d,]+\.\d{2})(?:\s{3,}|\s*$)', line)
                if cont_m:
                    vendor_part = clean_vendor(re.sub(r'\s+', ' ', line[:cont_m.start()].strip()))
                    # Only override pending_vendor if the continuation text looks like
                    # a real vendor name (contains letters, not just OCR noise/dates)
                    if (len(vendor_part) >= 4 and
                            re.search(r'[A-Za-z]{3,}', vendor_part) and
                            not re.search(r'^\d', vendor_part)):
                        pending_vendor = vendor_part
                    elif not pending_vendor:
                        # pending_vendor was empty (line was just two dates);
                        # only use this continuation if there's actual vendor text before the amount
                        if len(vendor_part) >= 2:
                            pending_vendor = vendor_part
                        else:
                            # No vendor text and no pending vendor — this is a sidebar amount, skip
                            continue
                    try:
                        amount = Decimal(cont_m.group(1).replace('$', '').replace(',', ''))
                        self._store_transaction(pending_date, pending_vendor, amount)
                    except Exception:
                        pass
                    pending_date = pending_vendor = None
                    continue

            # Find first date on this line (MM/DD not followed by /YY)
            date_m = re.search(DATE_PAT, line)
            if not date_m:
                # Fallback: partial date (MM/D) on lines with a negative amount = payment
                # Only match valid months 01-12 to avoid false positives like '20/2'
                partial_m = re.search(r'\b(0[1-9]|1[0-2])/(\d)\s+(.+?)\s+-([\$]?[\d,]+\.\d{2})', line)
                if partial_m:
                    partial_date = f"{partial_m.group(1)}/{partial_m.group(2)}0"
                    amt = Decimal(partial_m.group(4).replace('$','').replace(',',''))
                    self._store_transaction(partial_date, 'PAYMENT - THANK YOU', -amt)
                # No date — if line looks like a header/footer, reset pending
                # otherwise keep pending (vendor or amount may follow)
                if re.search(r'^\s*[A-Z][a-z]|Page \d|Statement|Account|Citi', stripped):
                    pending_date = pending_vendor = None
                continue

            date_str = date_m.group()
            after_first_date = line[date_m.end():]

            # Skip second date token if present
            second_date = re.match(r'\s+\d{2}/\d{2}', after_first_date)
            if second_date:
                after_first_date = after_first_date[second_date.end():]

            # Find the transaction amount: prefer first amount with gap (real txn),
            # fall back to last amount (end-of-line amounts without sidebar)
            amt_matches = list(p_amt.finditer(after_first_date))
            if not amt_matches:
                # No amount on this line — store as pending (amount may be on next line)
                vendor_raw = clean_vendor(re.sub(r'\s+', ' ', after_first_date.strip()))
                if not any(kw in vendor_raw.upper() for kw in skip_keywords):
                    pending_date   = date_str
                    # vendor_raw may be empty if this line was just two dates — that's OK,
                    # the next line will supply the vendor and amount
                    pending_vendor = vendor_raw if len(vendor_raw) >= 2 else ''
                else:
                    pending_date = pending_vendor = None
                continue

            # Gap match = amount followed by 3+ spaces (sidebar separator)
            p_amt_gap = re.compile(r'([-]?\$[\d,]+\.\d{2})\s{3,}')
            gap_matches = list(p_amt_gap.finditer(after_first_date))
            amt_m = gap_matches[0] if gap_matches else amt_matches[-1]

            amt_str = amt_m.group(1).replace('$', '').replace(',', '').strip()
            vendor_section = after_first_date[:amt_m.start()].strip()
            vendor_raw = clean_vendor(re.sub(r'\s+', ' ', vendor_section))

            if len(vendor_raw) < 2:
                pending_date = pending_vendor = None
                continue

            try:
                amount = Decimal(amt_str)
                self._store_transaction(date_str, vendor_raw, amount)
                pending_date = pending_vendor = None
            except Exception:
                pending_date = pending_vendor = None

    def _store_transaction(self, date_str, vendor_raw, amount):
        if self.closing_date:
            date_str = self._add_year_to_date(date_str, self.closing_date)
        txn_type = _classify_cc_transaction(vendor_raw, amount)
        if txn_type == 'payment':
            self.payments.append({
                'date': date_str,
                'description': 'PAYMENT - THANK YOU',
                'amount': abs(amount),
            })
            self.total_payments += abs(amount)
        elif txn_type == 'credit':
            # Normalize credit descriptions
            v = vendor_raw.upper()
            if 'AMAZON MKTPLACE' in v or 'MKTPLACE PMTS' in v:
                description = 'AMAZON MKTPLACE PMTS'
            elif 'WWW COSTCO' in v or 'COSTCO COM' in v:
                description = 'COSTCO RETURN'
            else:
                description = vendor_raw
            self.credits.append({
                'date': date_str,
                'description': description,
                'amount': abs(amount),
            })
        else:
            self.charges.append({
                'date': date_str,
                'vendor': vendor_raw,
                'amount': str(abs(amount)),
            })


    def load_from_dict(self, data):
        """
        Populate parser state from a pre-extracted data dict.
        Used when OCR output is too noisy (e.g. photographed/scanned statements).
        payments: [{'date': 'MM/DD/YY', 'description': str, 'amount': Decimal}]
        credits:  [{'date': 'MM/DD/YY', 'description': str, 'amount': Decimal}]
        charges:  [{'date': 'MM/DD/YY', 'vendor': str, 'amount': str}]
        """
        self.previous_balance       = Decimal(str(data.get('beginning_balance', 0)))
        self.new_balance            = Decimal(str(data.get('ending_balance', 0)))
        self.statement_new_charges  = Decimal(str(data.get('statement_new_charges', 0)))
        self.finance_charge         = Decimal(str(data.get('finance_charge', 0)))
        self.closing_date           = data.get('closing_date', None)
        self.statement_date         = (data.get('statement_period')
                                       or data.get('statement_date')
                                       or self.closing_date
                                       or '')
        self.payments = [
            {'date': p['date'], 'description': p.get('description', 'PAYMENT - THANK YOU'),
             'amount': Decimal(str(p['amount']))}
            for p in data.get('payments', [])
        ]
        self.total_payments = sum(p['amount'] for p in self.payments)
        self.credits = [
            {'date': c['date'], 'description': c.get('description', ''),
             'amount': Decimal(str(c['amount']))}
            for c in data.get('credits', [])
        ]
        self.charges = [
            {'date': c['date'], 'vendor': c['vendor'], 'amount': str(c['amount'])}
            for c in data.get('charges', [])
        ]
        self.client_name = data.get('client_name', self.client_name)

    def generate_report(self, check_payee_map=None, check_date_map=None):
        aggregated = self._aggregate_by_vendor(self.charges, date_fmt='%m/%d/%y')
        total_charges = sum(r['amount'] for r in aggregated)
        total_credits = sum(c['amount'] for c in self.credits)

        # Use parsed statement new charges directly; fall back to balance calculation
        statement_charges = None
        if self.statement_new_charges > Decimal('0'):
            statement_charges = self.statement_new_charges
        elif self.new_balance and self.previous_balance:
            statement_charges = (self.new_balance - self.previous_balance
                                 + self.total_payments + total_credits)
        aggregated, total_charges = _add_missing_row(aggregated, total_charges, statement_charges)

        report = _report_header(self.statement_type, self.client_name,
                                statement_date=getattr(self, 'statement_date', None) or self.closing_date or '')
        report += _summary_block([
            ('Previous Balance',  self.previous_balance),
            ('Payments',          self.total_payments),
            ('Credits / Returns', total_credits if total_credits else None),
            ('Purchases',         total_charges),
            ('Finance Charges',   self.finance_charge if self.finance_charge else None),
            ('New Balance',       self.new_balance),
        ])
        if self.payments:
            report += _payments_section(self.payments, self.total_payments)
        if self.credits:
            report += _credits_section(self.credits, total_credits)
        report += _charges_section(aggregated, total_charges)
        return report


class CitiSavingsParser(StatementParser):
    """
    Citi Business Savings account statements.

    Format mirrors CitiCheckingParser: date lines are MM/DD + transaction type +
    amounts; the vendor name follows on the next line.  Sections:
      Beginning Balance / Ending Balance in the account summary.
      Deposits and Credits (ACH CREDIT, DEPOSIT, ELECTRONIC CREDIT, INTEREST).
      Withdrawals and Debits (ACH DEBIT, WITHDRAWAL, TRANSFER).
    """
    statement_type = "Citi Business Savings"

    def __init__(self, pdf_path, client_name=None):
        super().__init__(pdf_path, client_name)
        self.statement_date = ''
        self.closing_date = None
        self.beginning_balance = Decimal('0')
        self.ending_balance = Decimal('0')
        self.deposits = []
        self.withdrawals = []
        self.total_deposits = Decimal('0')
        self.total_withdrawals = Decimal('0')

    def _detect_client(self):
        lines = self.text.split('\n')
        for line in lines[:20]:
            line_upper = line.upper()
            for name in KNOWN_CLIENTS:
                if name in line_upper:
                    return CLIENT_CANONICAL.get(name, name)
        return super()._detect_client()

    def parse(self):
        lines = self.text.split('\n')

        found_beginning = False
        found_ending = False
        for line in lines:
            if 'Beginning Balance:' in line and not found_beginning:
                bm = re.search(r'\$([\d,]+\.\d{2})', line)
                if bm:
                    self.beginning_balance = Decimal(bm.group(1).replace(',', ''))
                    found_beginning = True
            if 'Ending Balance:' in line and not found_ending:
                bm = re.search(r'\$([\d,]+\.\d{2})', line)
                if bm:
                    self.ending_balance = Decimal(bm.group(1).replace(',', ''))
                    found_ending = True
            if 'Statement Period' in line and not self.statement_date:
                m = re.search(r'(\w+ \d+ - \w+ \d+, \d{4})', line)
                if m:
                    self.statement_date = m.group(1)
                    ym = re.search(r'(\w+)\s+\d+,\s+(\d{4})$', m.group(1))
                    if ym:
                        month_names = {
                            'january':1,'february':2,'march':3,'april':4,
                            'may':5,'june':6,'july':7,'august':8,
                            'september':9,'october':10,'november':11,'december':12,
                        }
                        close_mm = month_names.get(ym.group(1).lower(), 1)
                        close_yy = str(int(ym.group(2)) % 100).zfill(2)
                        self.closing_date = f"{close_mm:02d}/28/{close_yy}"

        _CREDIT_TYPES = {'ACH CREDIT', 'ELECTRONIC CREDIT', 'INSTANT PAYMENT CREDIT',
                         'DEPOSIT', 'INTEREST'}
        _DEBIT_TYPES  = {'ACH DEBIT', 'WITHDRAWAL', 'TRANSFER OUT'}

        i = 0
        while i < len(lines):
            line = lines[i]
            # Stop at SAVINGS ACTIVITY boundary — savings interest belongs to a
            # different account and must not mix into the checking reconciliation.
            if re.match(r'\s*SAVINGS ACTIVITY\s*$', line):
                break
            # Skip summary/total lines that aren't real transactions
            if re.search(r'Total Debits/Credits', line, re.IGNORECASE):
                i += 1
                continue
            dm = re.match(r'^(\d{2}/\d{2})\s+(.+)', line)
            if dm:
                date = self._add_year_to_date(dm.group(1), self.closing_date) if self.closing_date else dm.group(1)
                rest = dm.group(2)

                trans_type = None
                # Check for CHECK NO: lines (same pattern as CitiCheckingParser)
                if 'CHECK NO:' in rest:
                    trans_type = 'CHECK'
                else:
                    for ttype in list(_CREDIT_TYPES) + list(_DEBIT_TYPES):
                        if ttype in rest:
                            trans_type = ttype
                            break

                if trans_type:
                    amounts = re.findall(r'([\d,]+\.\d{2})', line)
                    if len(amounts) >= 2:
                        amount = Decimal(amounts[-2].replace(',', ''))
                        vendor_line = lines[i + 1] if i + 1 < len(lines) else ''
                        vendor = re.split(r'\s{2,}', vendor_line.strip())[0].strip() or rest
                        vendor = self.normalize_vendor(vendor)

                        if trans_type in _CREDIT_TYPES:
                            self.deposits.append({'date': date, 'vendor': vendor, 'amount': amount})
                            self.total_deposits += amount
                        elif trans_type == 'CHECK':
                            check_num_m = re.search(r'CHECK NO:\s*(\d+)', rest)
                            check_num = check_num_m.group(1) if check_num_m else '?'
                            self.withdrawals.append({'date': date, 'vendor': f'Check #{check_num}', 'amount': amount})
                            self.total_withdrawals += amount
                        else:
                            self.withdrawals.append({'date': date, 'vendor': vendor, 'amount': amount})
                            self.total_withdrawals += amount
            i += 1

    def generate_report(self, check_payee_map=None, check_date_map=None):
        calc = self.beginning_balance + self.total_deposits - self.total_withdrawals
        ok   = self.ending_balance != Decimal('0') and abs(calc - self.ending_balance) < Decimal('0.01')

        report = _report_header(self.statement_type, self.client_name,
                                statement_date=self.statement_date)
        report += _summary_block([
            ('Beginning Balance', self.beginning_balance),
            ('Total Deposits',    self.total_deposits),
            ('Total Withdrawals', self.total_withdrawals),
            ('Ending Balance',    self.ending_balance),
        ])
        report += _balance_check(ok, calc)

        if self.deposits:
            deposit_rows = [{'vendor': d['vendor'], 'date': d['date'],
                             'amount': d['amount'], 'count': 1}
                            for d in self.deposits]
            report += _deposits_section(deposit_rows, self.total_deposits)

        if self.withdrawals:
            withdrawal_rows = [{'vendor': w['vendor'], 'date': w['date'],
                                'amount': w['amount'], 'count': 1}
                               for w in sorted(self.withdrawals,
                                               key=lambda x: _safe_date_key(x['date']))]
            report += _individual_section(withdrawal_rows, self.total_withdrawals,
                                          'WITHDRAWALS AND DEBITS')

        return report


