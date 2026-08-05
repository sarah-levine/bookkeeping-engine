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

from parsers.base import (
    StatementParser, _registry, KNOWN_CLIENTS, CLIENT_CANONICAL, CLIENT_CARDHOLDERS,
    _classify_cc_transaction, _is_known_cc_network_payment, contains_label,
)
from parsers.row_schema import TransactionRow
from parsers.report import *
from parsers.report import (
    _report_header, _summary_block, _balance_check, _is_balanced,
    _deposits_section, _charges_section, _checks_section,
    _adp_section, _payments_section, _credits_section,
    _cc_payments_section, _add_missing_row, _individual_section,
    _safe_date_key,
)

# Fee line keywords — these are already captured in Finance Charges
# (Total Fees for this Period). Skip them in the charges detail to avoid
# double-counting.
_FEE_KEYWORDS = ['ANNUAL FEE', 'LATE PAYMENT FEE', 'OVERLIMIT FEE',
                 'RETURNED PAYMENT FEE', 'CARD REPLACEMENT FEE',
                 'FOREIGN TRANSACTION FEE', 'CASH ADVANCE FEE',
                 'STATEMENT FEE', 'MEMBERSHIP FEE']


class AmexStatementParser(StatementParser):
    """
    American Express Business statements.
    Handles the zip-of-text-pages format AmEx delivers as well as standard PDFs.
    """
    statement_type = "American Express Business"

    def __init__(self, pdf_path, client_name=None):
        super().__init__(pdf_path, client_name)
        self.closing_date = None
        self.account_number = None
        self.previous_balance = None
        self.new_balance = None
        self.payments = []
        self.credits = []
        self.charges = []
        self.fees = Decimal('0')
        self.interest = Decimal('0')

    def _extract_text(self):
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
                return CLIENT_CANONICAL.get(name, name)
        return None

    def parse(self):
        lines = self.text.split('\n')

        # Account metadata
        for line in lines:
            if contains_label(line, 'Closing Date'):
                m = re.search(r'Closing\s+Date\s+(\d{2}/\d{2}/\d{2})', line)
                if m and not self.closing_date:
                    self.closing_date = m.group(1)
            if contains_label(line, 'Account Ending') and not self.account_number:
                m = re.search(r'Account\s+Ending\s+([\d\-]+)', line)
                if m:
                    self.account_number = m.group(1)

        # Previous / New Balance — grab last occurrence (Account Total section)
        prev_matches = re.findall(r'Previous\s+Balance\s*\r?\n\s*\$([0-9,]+\.\d{2})', self.text)
        if not prev_matches:
            prev_matches = re.findall(r'Previous\s+Balance\s+\$([0-9,]+\.\d{2})', self.text)
        new_matches = re.findall(r'^New\s+Balance\s+\$([0-9,]+\.\d{2})', self.text, re.MULTILINE)
        if not new_matches:
            new_matches = re.findall(r'New\s+Balance\s*\r?\n\s*\$([0-9,]+\.\d{2})', self.text)
        if not new_matches:
            new_matches = re.findall(r'New\s+Balance\s{2,}\$([0-9,]+\.\d{2})', self.text)
        if prev_matches:
            self.previous_balance = Decimal(prev_matches[-1].replace(',', ''))
        if new_matches:
            self.new_balance = Decimal(new_matches[-1].replace(',', ''))

        rows = self._extract_rows(lines)
        self._rows_to_legacy_shape(rows)

        # Fees / Interest — try the detailed section labels first, then fall back
        # to the summary "Finance Charges" line used on some AMEX statement formats.
        m = re.search(r'Total\s+Fees\s+for\s+this\s+Period\s+\$([0-9,]+\.\d{2})', self.text)
        if m:
            self.fees = Decimal(m.group(1).replace(',', ''))
        m = re.search(r'Total\s+Interest\s+Charged\s+for\s+this\s+Period\s+\$([0-9,]+\.\d{2})', self.text)
        if m:
            self.interest = Decimal(m.group(1).replace(',', ''))
        if self.fees == 0 and self.interest == 0:
            m = re.search(r'Finance\s+Charges[:\s]+\$\s*([0-9,]+\.\d{2})', self.text)
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

    def _extract_rows(self, lines):
        """Extract stage (see parsers/row_schema.py): raw statement text ->
        list[TransactionRow]. Two independent sequential sub-passes over
        `lines`, exactly mirroring the two loops this replaces (not fused
        into one state machine — they scan different line ranges for
        different reasons):

        1. Payments & Credits pass, over `lines[:charges_start]` only (see
           `charges_start` below, shared with pass 2). Extraction-native:
           the payment-keyword regex and the cardholder-aware `_credit_re`
           (scoped to `-$amount` lines only) decide type from raw text/sign
           alone, no classifier call. Previously scanned ALL of `lines`
           unscoped, which double-counted a negative-amount line inside the
           Charges section (matched independently by both passes) — fixed
           2026-07-14 (see REFACTORING_ROADMAP.md's "Closed: Fixed").
        2. Charges pass, over `lines[charges_start:]` only (starting at the
           standalone "New Charges" header — the earlier Payments/Credits
           block has a different inline-cardholder-prefix shape that would
           mis-split under this pass's trailing-junk stripper). Sequential
           and stateful: `current_cardholder` (set by standalone
           cardholder-name header lines) and `pending_date`/`pending_vendor`
           (carried across lines for the separate-line-amount fallback
           format) both persist purely within this pass. Calls
           `self.normalize_vendor()` then `_classify_cc_transaction()` —
           classifier-entangled, same shape as Chase and
           BankOfAmericaCreditCardParser's payments section — so type is
           decided here, not deferred to the adapter. Any negative amount
           or a 'credit' classification produces a `type='credit'` row;
           everything else produces `type='debit'` (this parser's charges
           bucket is always positive by construction — any negative line
           is explicitly diverted to credit above — so the standard
           sign-flip-for-debit, abs()-back-in-adapter convention applies,
           unlike BofA's charges bucket which preserves raw/mixed sign).
           `cardholder` has no TransactionRow slot, so it's carried via the
           `raw_description` overflow field (same repurposing precedent
           used for check numbers in Citi/Wells Fargo/BofA Checking) —
           payment/credit rows just set `raw_description` = `vendor` too,
           unused by the adapter for those two types, for schema
           consistency."""
        rows = []

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

        # Scope the Payments & Credits pass to stop before the Charges
        # section (computed here so both passes can use it) — otherwise a
        # negative-amount line inside "New Charges" gets matched by both
        # this pass and the Charges pass below, double-counting the same
        # credit. charges_start stays 0 (scan everything) if no "New
        # Charges" header is found, same tolerance the Charges pass below
        # already has for its own boundary.
        charges_start = 0
        for _i, _line in enumerate(lines):
            if _line.strip() == 'New Charges':
                charges_start = _i
                break

        # Payments and Credits
        for line in (lines[:charges_start] if charges_start else lines):
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
                rows.append(TransactionRow(
                    date=m.group(1), vendor=desc, raw_description=desc,
                    amount=Decimal(m.group(2).replace(',', '')), type='payment',
                ))
            # Credits (e.g. AMEX Wireless Credit, refunds, returns)
            # Handle multiple formats:
            # 1. Simple: DATE DESCRIPTION -$AMOUNT
            # 2. With cardholder: DATE CARDHOLDER DESCRIPTION -$AMOUNT
            # The -$ requirement already scopes this to genuine negative-amount
            # lines (real charges are always positive), so any match here is a
            # real credit — no extra keyword/cardholder heuristic needed. That
            # heuristic used to silently drop legitimate vendor refunds (e.g. a
            # merchant credit with no CREDIT/REFUND/RETURN/WIRELESS wording and
            # no configured cardholder prefix).
            mc = _credit_re.match(line)
            if mc and not m:
                desc = mc.group(3).strip()
                rows.append(TransactionRow(
                    date=mc.group(1), vendor=desc, raw_description=desc,
                    amount=Decimal(mc.group(4).replace(',', '')), type='credit',
                ))

        # Charges — multi-line: date+desc line, then optional phone/ref lines, then $amount line
        cardholders = CLIENT_CARDHOLDERS.get(self.client_name, [])
        cardholder_pattern = re.compile(
            r'^(' + '|'.join(re.escape(c) for c in cardholders) + r')\s*$'
        ) if cardholders else None

        # Match transactions with amount inline at end of line (e.g. "01/28/26   Extra Space   $38.00 ⧫")
        # Also matches negative amounts for credits (e.g. "-$1.98")
        txn_line_inline = re.compile(
            r'^(\d{2}/\d{2}/\d{2})\*?\s+(.+?)\s+(-?)\$([0-9,]+\.\d{2})\s*[⧫\*]?\s*$'
        )
        txn_line = re.compile(r'^(\d{2}/\d{2}/\d{2})\s+(.+)')
        amount_line = re.compile(r'^(-?)\$([0-9,]+\.\d{2})')
        skip_keywords = ['ELECTRONIC PAYMENT', 'AUTOPAY PAYMENT', 'PAYMENT RECEIVED',
                         'ONLINE PAYMENT', 'PAYMENT - THANK',
                         'Total Fees', 'Total Interest',
                         'Closing Date', 'Account Ending', 'Card Ending',
                         'Customer Care', 'Next Closing', 'AMEX Wireless Credit',
                         'Payments', 'Credits', 'New Charges', 'Total Payments',
                         'Payments/Credits',
                         'Detail', 'Summary', 'Amount']

        current_cardholder = None
        pending_date = None
        pending_vendor = None

        # charges_start was already computed above (needed by the
        # Payments/Credits pass too, to stop before this section — see the
        # comment there). Scoping this loop to start there, rather than
        # scanning the whole statement, also avoids walking the earlier
        # Payments/Credits Detail block, where each row has an inline
        # "CARDHOLDER   VENDOR" prefix (not a standalone cardholder header
        # line like the Charges section uses) — the trailing-junk stripper
        # below mis-splits those at the cardholder/vendor gap, producing
        # spurious duplicate credits under the cardholder's name.
        for line in lines[charges_start:]:
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
                is_negative = inline_m.group(3) == '-'
                txn_amount_str = inline_m.group(4)
                if any(kw in vendor_raw for kw in skip_keywords):
                    pending_date = None
                    pending_vendor = None
                    continue
                # Fees (annual, late payment, etc.) are captured in Finance
                # Charges via Total Fees — skip them here to avoid double-counting
                if any(kw in vendor_raw.upper() for kw in _FEE_KEYWORDS):
                    pending_date = None
                    pending_vendor = None
                    continue
                # Remove trailing ref numbers / extra merchant detail
                vendor_raw = re.sub(r'\s{2,}.*$', '', vendor_raw)
                vendor = self.normalize_vendor(vendor_raw)
                txn_amount = Decimal(txn_amount_str.replace(',', ''))
                txn_type = _classify_cc_transaction(vendor, txn_amount)
                # Negative amounts are always credits (e.g. refunds on cardholder cards)
                if is_negative or txn_type == 'credit':
                    rows.append(TransactionRow(
                        date=date_str, vendor=vendor, raw_description=vendor,
                        amount=txn_amount, type='credit',
                    ))
                else:
                    rows.append(TransactionRow(
                        date=date_str, vendor=vendor, raw_description=current_cardholder or '',
                        amount=-txn_amount, type='debit',
                    ))
                pending_date = None
                pending_vendor = None
                continue

            # Fallback: separate-line amount
            amt_m = amount_line.match(stripped)
            if amt_m and pending_date and pending_vendor:
                is_negative = amt_m.group(1) == '-'
                vendor = self.normalize_vendor(pending_vendor)
                txn_amount = Decimal(amt_m.group(2).replace(',', ''))
                txn_type = _classify_cc_transaction(vendor, txn_amount)
                if is_negative or txn_type == 'credit':
                    rows.append(TransactionRow(
                        date=pending_date, vendor=vendor, raw_description=vendor,
                        amount=txn_amount, type='credit',
                    ))
                else:
                    rows.append(TransactionRow(
                        date=pending_date, vendor=vendor, raw_description=current_cardholder or '',
                        amount=-txn_amount, type='debit',
                    ))
                pending_date = None
                pending_vendor = None
                continue

            txn_m = txn_line.match(stripped)
            if txn_m:
                if any(kw in txn_m.group(2) for kw in skip_keywords):
                    pending_date = None
                    pending_vendor = None
                    continue
                # Fees are captured in Finance Charges — skip them here
                if any(kw in txn_m.group(2).upper() for kw in _FEE_KEYWORDS):
                    pending_date = None
                    pending_vendor = None
                    continue
                pending_date = txn_m.group(1)
                vendor_raw = txn_m.group(2)
                # Normal extraction: remove amount and trailing state codes
                vendor_raw = re.sub(r'\s{2,}.*$', '', vendor_raw)
                vendor_raw = re.sub(r'\s+[A-Z][A-Z\s]+[A-Z]{2}\s*$', '', vendor_raw).strip()
                pending_vendor = vendor_raw

        return rows

    def _rows_to_legacy_shape(self, rows):
        """Adapter: list[TransactionRow] -> self.payments/self.credits/
        self.charges in their existing dict shapes."""
        for row in rows:
            if row.type == 'payment':
                self.payments.append({'date': row.date, 'description': row.vendor, 'amount': row.amount})
            elif row.type == 'credit':
                self.credits.append({'date': row.date, 'description': row.vendor, 'amount': row.amount})
            else:  # 'debit' (charges)
                self.charges.append({
                    'date': row.date,
                    'cardholder': row.raw_description,
                    'vendor': row.vendor,
                    'amount': abs(row.amount),
                })

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
            ok = _is_balanced(calc, self.new_balance)
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
            if contains_label(line, 'Beginning Balance as of') and self.beginning_balance is None:
                m = re.search(r'\$([0-9,]+\.\d{2})', line)
                if m:
                    self.beginning_balance = Decimal(m.group(1).replace(',', ''))
            if contains_label(line, 'Ending Balance as of') and self.ending_balance is None:
                m = re.search(r'\$([0-9,]+\.\d{2})', line)
                if m:
                    self.ending_balance = Decimal(m.group(1).replace(',', ''))
            if contains_label(line, 'Statement Date:') and not self.statement_date:
                m = re.search(r'Statement\s+Date:\s+(\d{2}/\d{2}/\d{4})', line)
                if m:
                    self.statement_date = m.group(1)
            if contains_label(line, 'Account Ending:') and not self.account_number:
                m = re.search(r'Account\s+Ending:\s+\*?(\d+)', line)
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

        rows = self._extract_rows(lines)
        self._rows_to_legacy_shape(rows)

        # Remove check debits — they appear in Checks Paid Summary and are
        # fully accounted for in the CHECKS section. Filter by description keyword.
        self.debits = [d for d in self.debits
                       if 'CHECK' not in d['description'].upper()]

    def _extract_rows(self, lines):
        """Extract stage (see parsers/row_schema.py): raw statement text ->
        list[TransactionRow]. is_credit/is_debit come directly from text
        labels literally printed on the transaction line (': CREDIT',
        ': DEBIT', 'INTEREST DEPOSIT', etc.) — extraction-native, no
        classifier entanglement.

        Interest-deposit lines are NOT converted to rows at all —
        self.interest_earned is a running scalar total (never rendered as
        individual line items), the same category as self.service_fees/
        self.finance_charge in other parsers, so it's accumulated here as a
        direct side effect, same as those.

        Checks (from the Checks Paid Summary section) produce type='check'
        rows with vendor = check number (payee is always blank at parse
        time), same repurposing precedent as Citi Checking/Wells Fargo
        Checking/BofA Checking.

        Amounts are always positive at this parser (`amount = abs(...)`
        for both credits and debits) — the Citi/Chase-family convention,
        not BofA's raw-sign-preserved convention — so the standard
        sign-flip-for-debit, abs()-back-in-adapter pattern applies."""
        rows = []
        in_checks_summary = False
        skip_keywords = ['Beginning Balance', 'Ending Balance', 'Date         Description',
                         'Continued on next page', 'Account Activity', 'Accounts offered by',
                         'Statement Date:', 'Account Address', 'Contact Us']

        i = 0
        while i < len(lines):
            line = lines[i]

            if contains_label(line, 'Checks Paid Summary'):
                in_checks_summary = True
                i += 1
                continue

            if in_checks_summary:
                m = re.match(r'^\s*(\d+)\s+(\d{2}/\d{2}/\d{4})\s+\$([0-9,]+\.\d{2})', line)
                if m:
                    check_num = m.group(1)
                    rows.append(TransactionRow(
                        date=self._fmt_date(m.group(2)), vendor=check_num, raw_description=check_num,
                        amount=-Decimal(m.group(3).replace(',', '')), type='check',
                    ))
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
                    rows.append(TransactionRow(
                        date=date, vendor=desc, raw_description=desc,
                        amount=amount, type='credit',
                    ))
            else:
                amount = abs(txn_amount)
                rows.append(TransactionRow(
                    date=date, vendor=desc, raw_description=desc,
                    amount=-amount, type='debit',
                ))

            i = j

        return rows

    def _rows_to_legacy_shape(self, rows):
        """Adapter: list[TransactionRow] -> self.credits/self.debits/
        self.checks in their existing dict shapes."""
        for row in rows:
            if row.type == 'credit':
                self.credits.append({'date': row.date, 'description': row.vendor, 'amount': row.amount})
            elif row.type == 'debit':
                self.debits.append({'date': row.date, 'description': row.vendor, 'amount': abs(row.amount)})
            else:  # 'check'
                self.checks.append({
                    'check_number': row.vendor,
                    'date': row.date,
                    'amount': abs(row.amount),
                    'payee': '',
                })

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
            elif (_is_known_cc_network_payment(d_upper) or 'AUTOPAY' in d_upper or
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
            ok = _is_balanced(calc, self.ending_balance)
            report += _balance_check(ok, calc)

        report += _deposits_section(deposits, total_deposits, title='CREDITS / DEPOSITS')
        report += _charges_section(withdrawals, total_withdrawals, title='WITHDRAWALS AND DEBITS')
        if checks:
            report += _checks_section(checks, total_checks)
        if adp:
            report += _adp_section(adp, total_adp)
        return report


from parsers.registry import register  # noqa: E402
register("amex", "American Express Business", AmexStatementParser, is_credit_card=True)
register("amex_checking", "American Express Business Checking", AmexCheckingParser)


