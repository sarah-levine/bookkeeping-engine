import sys
import re
import os
import json
from pathlib import Path
from decimal import Decimal
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from parsers.base import (
    StatementParser, _registry, KNOWN_CLIENTS, CLIENT_CANONICAL,
    _classify_cc_transaction,
)
from parsers.row_schema import TransactionRow
from parsers.report import *
from parsers.report import (
    _report_header, _summary_block, _balance_check, _is_balanced,
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
                    # Extract closing month/day/year from the closing half, e.g.
                    # "January 1 - January 31, 2026" -> month=January, day=31, year=2026.
                    ymd = re.search(r'(\w+)\s+(\d+),\s+(\d{4})$', m.group(1))
                    if ymd:
                        month_names = {'january':1,'february':2,'march':3,'april':4,
                                       'may':5,'june':6,'july':7,'august':8,
                                       'september':9,'october':10,'november':11,'december':12,
                                       'jan':1,'feb':2,'mar':3,'apr':4,
                                       'jun':6,'jul':7,'aug':8,
                                       'sep':9,'oct':10,'nov':11,'dec':12}
                        close_mm = month_names.get(ymd.group(1).lower(), 1)
                        close_dd = int(ymd.group(2))
                        close_yy = str(int(ymd.group(3)) % 100).zfill(2)
                        self.closing_date = f"{close_mm:02d}/{close_dd:02d}/{close_yy}"

        rows = self._extract_rows(lines)
        self._rows_to_legacy_shape(rows)

    def _extract_rows(self, lines):
        """Extract stage (see parsers/row_schema.py): raw statement text ->
        list[TransactionRow]. Classification comes straight from an explicit
        type keyword in the raw text (like Northern Trust and Citi Savings,
        not entangled like Chase). Vendor computation is purely per-type,
        decided only by which keyword matched — no client-config logic here;
        the ADP / no_aggregate_vendors / CREDIT CRD-AUTOPAY cascade for
        ACH DEBIT rows is a Classify-stage decision (config-dependent,
        mutates vendor) and lives in _rows_to_legacy_shape() below, same
        precedent as Northern Trust's Square line-position remapping.

        Preserves the exact index-based while-loop mechanics of the
        original code, including the CHECK NO: override running
        unconditionally *after* the type-keyword loop (so it wins over any
        earlier match), and ELECTRONIC CREDIT being the one credit type
        that keeps the full, unsplit vendor-lookahead line rather than
        splitting on 2+ spaces like ACH DEBIT/ACH CREDIT do."""
        rows = []
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
                elif trans_type == 'INSTANT PAYMENT CREDIT':
                    vendor = 'Merchant Deposits'
                elif trans_type == 'DEPOSIT':
                    vendor = 'Deposit'
                elif trans_type == 'CHECK':
                    check_num_m = re.search(r'CHECK NO:\s*(\d+)', rest)
                    check_num = check_num_m.group(1) if check_num_m else '?'
                    rows.append(TransactionRow(
                        date=date, vendor=check_num, raw_description=rest,
                        amount=-amount, type='check',
                    ))
                    i += 1
                    continue
                # ELECTRONIC CREDIT: no override, vendor stays the full,
                # unsplit vendor-lookahead line.

                if trans_type in ('INSTANT PAYMENT CREDIT', 'DEPOSIT', 'ACH CREDIT', 'ELECTRONIC CREDIT'):
                    rows.append(TransactionRow(
                        date=date, vendor=vendor, raw_description=rest,
                        amount=amount, type='credit',
                    ))
                else:  # ACH DEBIT
                    rows.append(TransactionRow(
                        date=date, vendor=vendor, raw_description=rest,
                        amount=-amount, type='debit',
                    ))

            i += 1
        return rows

    def _rows_to_legacy_shape(self, rows):
        """Adapter: list[TransactionRow] -> self.credits/self.charges/
        self.adp_transactions/self.credit_card_payments/self.checks in
        their existing dict shapes. 'debit' rows run the ADP -> config
        no_aggregate_vendors tag -> CREDIT CRD/AUTOPAY cascade, exactly as
        before (order matters: the no-agg tag is applied, and can still be
        present, when the CREDIT CRD/AUTOPAY check runs next). All three
        debit-derived buckets add into the same self.total_charges, matching
        current behavior — it's one shared running total, not per-bucket.
        'check' rows carry their check number in row.vendor (set that way
        in _extract_rows for lack of a dedicated row field) and are
        rebuilt into the checks bucket's own {'date', 'check_num', 'amount'}
        shape, which has no 'vendor' key at all."""
        for row in rows:
            if row.type == 'credit':
                self.credits.append({'date': row.date, 'vendor': row.vendor, 'amount': row.amount})
                self.total_credits += row.amount
            elif row.type == 'check':
                self.checks.append({'date': row.date, 'check_num': row.vendor, 'amount': abs(row.amount)})
                self.total_checks += abs(row.amount)
            else:  # 'debit'
                vendor = row.vendor
                amount = abs(row.amount)
                if 'ADP' in vendor.upper():
                    self.adp_transactions.append({'date': row.date, 'vendor': vendor, 'amount': amount})
                    self.total_charges += amount
                    continue
                no_agg = (_registry.get_config(self.client_name) or {}).get('no_aggregate_vendors', [])
                if any(v.upper() in vendor.upper() for v in no_agg):
                    # Tag with date to prevent aggregation; display strips the tag.
                    vendor = f'{vendor}|{row.date}'
                if 'CREDIT CRD' in vendor.upper() or 'AUTOPAY' in vendor.upper():
                    self.credit_card_payments.append({'date': row.date, 'vendor': vendor, 'amount': amount})
                    self.total_charges += amount
                    continue
                self.charges.append({'date': row.date, 'vendor': vendor, 'amount': amount})
                self.total_charges += amount

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
        ok = _is_balanced(calc, self.new_balance)
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

    # Tolerate common OCR digit confusions (O/o -> 0, l/I -> 1) inside a
    # matched date substring only — never applied to the full statement
    # text, so a clean digital PDF's dates are completely unaffected.
    _OCR_DIGIT = '[0-9OolI]'

    @staticmethod
    def _clean_ocr_date(s: str) -> str:
        return s.translate(str.maketrans({'O': '0', 'o': '0', 'l': '1', 'I': '1'}))

    def _extract_closing_date(self):
        """Closing date via "Billing Period: MM/DD/YY-MM/DD/YY" (the later
        of the two dates), falling back to "as of MM/DD/YY" near the new-
        balance summary if the billing-period line is too OCR-garbled to
        recover. Returns None if neither is found.

        Searches the whole statement with whitespace collapsed to single
        spaces, not line-by-line — a scanned/OCR'd statement can split
        "Billing Period" and its dates across a line break (or introduce
        irregular spacing), which a per-line, single-space-literal match
        would silently miss. The billing-period date digits also tolerate
        common OCR confusions (see _OCR_DIGIT) — but on a real scanned Citi
        Costco fixture the billing-period text OCR'd to
        "Billing Period: O3/2O//6-O4/2dt26" (a dropped digit and stray
        letters, not just O/l confusion) and was unrecoverable even with
        that tolerance. The same closing date reliably OCR'd cleanly a few
        lines later as "$209.49 as of 04/20/26" (from "New balance as of
        <date>: $<amount>"), so that's the fallback source.
        """
        normalized_text = re.sub(r'\s+', ' ', self.text)

        date_pat = rf'({self._OCR_DIGIT}{{2}}/{self._OCR_DIGIT}{{2}}/{self._OCR_DIGIT}{{2}})'
        m = re.search(rf'Billing\s*Period.{{0,20}}?{date_pat}\s*-\s*{date_pat}', normalized_text)
        if m:
            return self._clean_ocr_date(m.group(2))  # closing date is the later one

        m = re.search(r'as of\s+(\d{2}/\d{2}/\d{2})', normalized_text)
        if m:
            return m.group(1)

        return None

    def parse(self):
        raw_lines = self.text.split('\n')

        # ── Extract billing period / closing date ────────────────────────────
        closing_date = self._extract_closing_date()
        if closing_date:
            self.closing_date = closing_date

        # ── Balances ─────────────────────────────────────────────────────────
        self.statement_new_charges = Decimal('0')
        for line in raw_lines:
            if re.search(r'Previous\s+[Bb]alance', line) and self.previous_balance == Decimal('0'):
                m = re.search(r'-?\$?\s*([\d,]+\.\d{2})', line)
                if m:
                    self.previous_balance = abs(Decimal(m.group(1).replace(',', '')))
            # "New balance as of MM/DD/YY: $XXX" or "New Balance  $XXX" — the
            # -layout column gap between "New" and "balance" can be many
            # spaces wide (not always one), e.g. "New    balance    $4,098.16"
            # in the Account Summary box vs "New balance as of..." in the
            # rewards sidebar, so match on \s+ not a literal single space.
            if re.search(r'New\s+[Bb]alance', line) and self.new_balance == Decimal('0'):
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

        rows = self._extract_rows(raw_lines)
        self._rows_to_legacy_shape(rows)

    def _extract_rows(self, raw_lines):
        """Extract stage (see parsers/row_schema.py): raw statement text ->
        list[TransactionRow]. One long, single-pass, deeply stateful scan —
        genuinely order-dependent (pending_date/pending_vendor carry across
        lines for three different fallback shapes, plus a fourth
        partial-date fallback), so it stays as one sequential loop here,
        same category as BofA Checking's continuation accumulation and
        Amex Statement's Charges pass.

        _make_row() is a nested closure replacing the 4 call sites that
        used to call self._store_transaction() directly — it's
        classifier-entangled (_classify_cc_transaction() decides the row's
        type, same shape as Chase/BofA-payments/Amex-charges) and applies
        the hardcoded credit-description overrides (AMAZON MKTPLACE PMTS /
        COSTCO RETURN) inline, so classification can't be deferred to the
        adapter. Sign convention: Citi/Chase-family "always positive" —
        every bucket stores abs(amount) regardless of type today, so
        payment/credit rows store abs(amount) (matching TransactionRow's
        documented default) and debit (charge) rows store -abs(amount),
        sign-flipped back to positive in the adapter."""
        rows = []

        def _make_row(date_str, vendor_raw, amount):
            if self.closing_date:
                date_str = self._add_year_to_date(date_str, self.closing_date)
            txn_type = _classify_cc_transaction(vendor_raw, amount)
            if txn_type == 'payment':
                rows.append(TransactionRow(
                    date=date_str, vendor='PAYMENT - THANK YOU', raw_description=vendor_raw,
                    amount=abs(amount), type='payment',
                ))
            elif txn_type == 'credit':
                # Normalize credit descriptions
                v = vendor_raw.upper()
                if 'AMAZON MKTPLACE' in v or 'MKTPLACE PMTS' in v:
                    description = 'AMAZON MKTPLACE PMTS'
                elif 'WWW COSTCO' in v or 'COSTCO COM' in v:
                    description = 'COSTCO RETURN'
                else:
                    description = vendor_raw
                rows.append(TransactionRow(
                    date=date_str, vendor=description, raw_description=vendor_raw,
                    amount=abs(amount), type='credit',
                ))
            else:
                rows.append(TransactionRow(
                    date=date_str, vendor=vendor_raw, raw_description=vendor_raw,
                    amount=-abs(amount), type='debit',
                ))

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
                    _make_row(pending_date, pending_vendor, amount)
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
                        _make_row(pending_date, pending_vendor, amount)
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
                    _make_row(partial_date, 'PAYMENT - THANK YOU', -amt)
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
                _make_row(date_str, vendor_raw, amount)
                pending_date = pending_vendor = None
            except Exception:
                pending_date = pending_vendor = None

        return rows

    def _rows_to_legacy_shape(self, rows):
        """Adapter: list[TransactionRow] -> self.payments/self.credits/
        self.charges in their existing dict shapes. self.total_payments is
        incremented in lockstep as each payment row is processed, matching
        _store_transaction's original behavior of accumulating it directly
        rather than deriving it by summing self.payments afterward."""
        for row in rows:
            if row.type == 'payment':
                self.payments.append({'date': row.date, 'description': row.vendor, 'amount': row.amount})
                self.total_payments += row.amount
            elif row.type == 'credit':
                self.credits.append({'date': row.date, 'description': row.vendor, 'amount': row.amount})
            else:  # 'debit' (charges)
                self.charges.append({'date': row.date, 'vendor': row.vendor, 'amount': abs(row.amount)})


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
            {'date': c['date'], 'vendor': c['vendor'], 'amount': Decimal(str(c['amount']))}
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
        all_lines = self.text.split('\n')

        # A savings statement can arrive bundled inside a combined
        # checking+savings PDF (same account, one file). The checking
        # section's own "Beginning Balance:"/"Ending Balance:" and
        # transaction lines appear BEFORE the "SAVINGS ACTIVITY" section
        # header in that case — scope everything below to start there so
        # this parser never picks up the checking account's numbers as its
        # own. Falls back to the full text (savings_start=0) for a
        # standalone savings-only statement with no such header.
        savings_start = 0
        for idx, line in enumerate(all_lines):
            if re.match(r'\s*SAVINGS ACTIVITY\s*$', line):
                savings_start = idx
                break
        lines = all_lines[savings_start:]

        # Statement Period is a document-level property (same value on every
        # page, including pages before SAVINGS ACTIVITY) — search the full,
        # unscoped text for it. Some statements only print it on earlier
        # pages and don't repeat it after the savings section.
        for line in all_lines:
            if 'Statement Period' in line and not self.statement_date:
                m = re.search(r'(\w+ \d+ - \w+ \d+, \d{4})', line)
                if m:
                    self.statement_date = m.group(1)
                    # Extract month/day/year from the closing half, e.g.
                    # "Jun 1 - Jun 30, 2026" -> month=Jun, day=30, year=2026.
                    ymd = re.search(r'(\w+)\s+(\d+),\s+(\d{4})$', m.group(1))
                    if ymd:
                        month_names = {
                            'january':1,'february':2,'march':3,'april':4,
                            'may':5,'june':6,'july':7,'august':8,
                            'september':9,'october':10,'november':11,'december':12,
                            'jan':1,'feb':2,'mar':3,'apr':4,
                            'jun':6,'jul':7,'aug':8,
                            'sep':9,'oct':10,'nov':11,'dec':12,
                        }
                        close_mm = month_names.get(ymd.group(1).lower(), 1)
                        close_dd = int(ymd.group(2))
                        close_yy = str(int(ymd.group(3)) % 100).zfill(2)
                        self.closing_date = f"{close_mm:02d}/{close_dd:02d}/{close_yy}"

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

        rows = self._extract_rows(lines)
        self._rows_to_legacy_shape(rows)

    _CREDIT_TYPES = {'ACH CREDIT', 'ELECTRONIC CREDIT', 'INSTANT PAYMENT CREDIT',
                     'DEPOSIT', 'INTEREST'}
    _DEBIT_TYPES  = {'ACH DEBIT', 'WITHDRAWAL', 'TRANSFER OUT'}

    def _extract_rows(self, lines):
        """Extract stage (see parsers/row_schema.py): raw statement text
        (already scoped to the SAVINGS ACTIVITY section by parse(), unlike
        the unscoped statement_date/closing_date search above it) -> list of
        TransactionRow. Classification comes straight from an explicit type
        keyword in the raw text (like Northern Trust's "ACH Debit"/"ACH
        Credit" labels, unlike Chase's entangled case) — genuine
        extraction-time signal, no shared classifier call needed.

        Preserves the exact index-based while-loop mechanics of the
        original code: a matched transaction line's vendor comes from
        lines[i + 1] (the following line), and the loop does NOT skip past
        that consumed line (i only advances by 1, not 2) — it relies on
        vendor lines never themselves matching the ^MM/DD transaction-line
        pattern. Not "fixed" to i += 2 here, since this migration's job is
        byte-identical behavior, not closing a latent edge case."""
        rows = []
        i = 0
        while i < len(lines):
            line = lines[i]
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
                    for ttype in list(self._CREDIT_TYPES) + list(self._DEBIT_TYPES):
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

                        if trans_type in self._CREDIT_TYPES:
                            rows.append(TransactionRow(
                                date=date, vendor=vendor, raw_description=rest,
                                amount=amount, type='credit',
                            ))
                        elif trans_type == 'CHECK':
                            check_num_m = re.search(r'CHECK NO:\s*(\d+)', rest)
                            check_num = check_num_m.group(1) if check_num_m else '?'
                            rows.append(TransactionRow(
                                date=date, vendor=f'Check #{check_num}', raw_description=rest,
                                amount=-amount, type='check',
                            ))
                        else:
                            rows.append(TransactionRow(
                                date=date, vendor=vendor, raw_description=rest,
                                amount=-amount, type='debit',
                            ))
            i += 1
        return rows

    def _rows_to_legacy_shape(self, rows):
        """Adapter: list[TransactionRow] -> self.deposits/self.withdrawals
        in their existing dict shapes ({'date', 'vendor', 'amount'}, always
        positive — this parser's own convention, direction is bucket
        membership not sign, matching CitiCheckingParser's convention).
        'check' and 'debit' rows both land in self.withdrawals, matching
        current behavior (checks aren't a separate bucket here)."""
        for row in rows:
            if row.type == 'credit':
                self.deposits.append({'date': row.date, 'vendor': row.vendor, 'amount': row.amount})
                self.total_deposits += row.amount
            else:  # 'check' or 'debit' -> withdrawals
                self.withdrawals.append({'date': row.date, 'vendor': row.vendor, 'amount': abs(row.amount)})
                self.total_withdrawals += abs(row.amount)

    def generate_report(self, check_payee_map=None, check_date_map=None):
        calc = self.beginning_balance + self.total_deposits - self.total_withdrawals
        ok   = self.ending_balance != Decimal('0') and _is_balanced(calc, self.ending_balance)

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


from parsers.registry import register  # noqa: E402
register("citi_checking", "Citi Business Checking", CitiCheckingParser)
register("citi_savings", "Citi Business Savings", CitiSavingsParser)
register("citi_visa_costco", "Citi Costco Anywhere Visa", CitiVisaCostcoParser, is_credit_card=True)


