import re
import sys
from decimal import Decimal
from datetime import datetime

from parsers.base import StatementParser, _registry, KNOWN_CLIENTS, _classify_cc_transaction, contains_label
from parsers.report import (
    _report_header, _summary_block, _balance_check, _is_balanced,
    _payments_section, _credits_section,
    _add_missing_row, _charges_section,
)

_CAP1_MONTH = {
    'jan': '01', 'feb': '02', 'mar': '03', 'apr': '04',
    'may': '05', 'jun': '06', 'jul': '07', 'aug': '08',
    'sep': '09', 'oct': '10', 'nov': '11', 'dec': '12',
    'january': '01', 'february': '02', 'march': '03', 'april': '04',
    'june': '06', 'july': '07', 'august': '08', 'september': '09',
    'october': '10', 'november': '11', 'december': '12',
}

# Transaction line patterns for pdftotext -layout output.
# The real (fixture-verified) Capital One layout is per-cardholder tables
# with TWO "Mon DD" dates -- Trans Date then Post Date -- before the
# description and amount, e.g. "Apr 10   Apr 11   CONSUMER REPORTS...
# $39.00". Tried first since it's the confirmed real shape. The single-date
# patterns below are kept as a fallback for any other statement layout
# this parser might see (unverified against a real fixture).
_TXN_ABBREV_TWO_DATE = re.compile(
    r'^([A-Za-z]{3,9})\s+(\d{1,2})\s+[A-Za-z]{3,9}\s+\d{1,2}\s+(.+?)\s+(-?\s*\$?[0-9,]+\.\d{2})\s*$'
)
_TXN_ABBREV = re.compile(
    r'^([A-Za-z]{3,9})\s+(\d{1,2})\s+(.+?)\s+(-?\s*\$?[0-9,]+\.\d{2})\s*$'
)
_TXN_NUMERIC = re.compile(
    r'^(\d{1,2}/\d{1,2})\s+(.+?)\s+(-?\s*\$?[0-9,]+\.\d{2})\s*$'
)

# Column headers and summary labels that should not be parsed as transactions
_SKIP_PHRASES = frozenset([
    'DATE', 'DESCRIPTION', 'CATEGORY', 'CARD', 'AMOUNT',
    'ACCOUNT ACTIVITY', 'ACCOUNT SUMMARY', 'TRANSACTIONS',
    'PREVIOUS BALANCE', 'NEW BALANCE', 'PAYMENTS AND OTHER CREDITS',
    'FEES', 'INTEREST CHARGED',
])


class CapitalOneParser(StatementParser):
    """
    Capital One business credit cards (e.g. Spark Business Unlimited).

    Statement format (pdftotext -layout):
      - 'Statement Ending Month DD, YYYY' identifies the closing date.
      - Account summary block: Previous Balance / New Balance.
      - Transaction table columns: Date | Description | Category | Card | Amount
        Date is 'Mon DD' (e.g. Apr 15); negative amounts are payments or credits.
      - 'Payment from US BANK NA' appears as a negative-amount payment line.
    """
    statement_type = "Capital One Business Credit Card"

    def __init__(self, pdf_path, client_name=None):
        super().__init__(pdf_path, client_name)
        self.closing_date = None
        self.previous_balance = None
        self.new_balance = None
        self.payments = []
        self.credits = []
        self.charges = []
        self.fees = Decimal('0')
        self.interest = Decimal('0')

    def _parse_closing_date(self):
        """Parse 'Statement Ending May 8, 2026' → '05/08/26'."""
        # Verbose month: "Statement Ending May 8, 2026"
        m = re.search(
            r'Statement\s+Ending\s+([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})',
            self.text, re.IGNORECASE,
        )
        if m:
            month_num = _CAP1_MONTH.get(m.group(1).lower())
            if month_num:
                day = int(m.group(2))
                yy = m.group(3)[-2:]
                return f"{month_num}/{day:02d}/{yy}"
        # Numeric: "Statement Ending 05/08/2026" or "05/08/26"
        m = re.search(
            r'Statement\s+Ending\s+(\d{1,2}/\d{1,2}/\d{2,4})',
            self.text, re.IGNORECASE,
        )
        if m:
            raw = m.group(1)
            parts = raw.split('/')
            if len(parts) == 3:
                mm, dd, yr = parts
                yy = yr[-2:]
                return f"{int(mm):02d}/{int(dd):02d}/{yy}"
        # Billing-cycle range: "Apr 08, 2026 - May 08, 2026 | 31 days in
        # Billing Cycle" -- some Capital One statements never print
        # "Statement Ending" at all; the cycle's end date is the closing
        # date. Takes the second (later) date in the range.
        m = re.search(
            r'[A-Za-z]+\s+\d{1,2},?\s+\d{4}\s*-\s*([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})',
            self.text,
        )
        if m:
            month_num = _CAP1_MONTH.get(m.group(1).lower())
            if month_num:
                day = int(m.group(2))
                yy = m.group(3)[-2:]
                return f"{month_num}/{day:02d}/{yy}"
        return None

    def _mmdd_from_abbrev(self, abbrev, day):
        """Convert ('Apr', '15') → '04/15', or None if month not recognized."""
        num = _CAP1_MONTH.get(abbrev.lower())
        return f"{num}/{int(day):02d}" if num else None

    @staticmethod
    def _parse_amount(raw):
        """Convert '-$1,234.56', '$-1,234.56', '- $1,234.56' (a real fixture's
        payment line puts a space between the minus sign and the dollar
        sign), or '$1,234.56' to signed Decimal."""
        s = re.sub(r'\s+', '', raw)
        negative = s.startswith('-')
        s = s.lstrip('-').lstrip('$').replace(',', '')
        try:
            val = Decimal(s)
            return -val if negative else val
        except Exception:
            return None

    def parse(self):
        self.closing_date = self._parse_closing_date()
        lines = self.text.split('\n')

        # ── Account summary ──────────────────────────────────────────────────
        for line in lines:
            # Search for the dollar amount only in the text AFTER the label
            # match, not the whole line. The two-column Account Summary
            # block sits beside the payment-coupon/warning text on the same
            # visual row, and pdftotext -layout merges both onto one output
            # line -- e.g. "...may have to pay a $39.00 late fee...  New
            # Balance  = $61.35" is one real line, where $39.00 is unrelated
            # left-column text and $61.35 (after "New Balance") is the real
            # value. Searching the whole line grabbed whichever $ came
            # first, which is only ever correct by accident.
            m_prev = contains_label(line, 'Previous Balance')
            if m_prev and self.previous_balance is None:
                label_end = re.search(r'\s+'.join(re.escape(w) for w in 'Previous Balance'.split()), line).end()
                m = re.search(r'\$\s*([0-9,]+\.\d{2})', line[label_end:])
                if m:
                    self.previous_balance = Decimal(m.group(1).replace(',', ''))

            m_new = re.search(r'\bNew\s+Balance\b', line)
            if m_new and 'Minimum' not in line and self.new_balance is None:
                m = re.search(r'\$\s*([0-9,]+\.\d{2})', line[m_new.end():])
                if m:
                    self.new_balance = Decimal(m.group(1).replace(',', ''))

            m_fees = re.search(r'\bFees?\b', line)
            if m_fees and self.fees == 0:
                m = re.search(r'\$\s*([0-9,]+\.\d{2})', line[m_fees.end():])
                if m:
                    v = Decimal(m.group(1).replace(',', ''))
                    if v > 0:
                        self.fees = v

            m_int = contains_label(line, 'Interest Charged')
            if m_int and self.interest == 0:
                label_end = re.search(r'\s+'.join(re.escape(w) for w in 'Interest Charged'.split()), line).end()
                m = re.search(r'\$\s*([0-9,]+\.\d{2})', line[label_end:])
                if m:
                    self.interest = Decimal(m.group(1).replace(',', ''))

        # ── Transactions ─────────────────────────────────────────────────────
        # Real (fixture-verified) layout: per-cardholder tables of
        # Trans Date | Post Date | Description | Amount. The single-date
        # patterns are an unverified fallback for any other layout.
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.upper() in _SKIP_PHRASES:
                continue

            date_str = None
            mid = None
            amount_raw = None

            # "Apr 10  Apr 11  Description  $Amount" (Trans Date, Post Date)
            m = _TXN_ABBREV_TWO_DATE.match(stripped)
            if m:
                abbrev, day, mid_raw, amount_raw = m.groups()
                mmdd = self._mmdd_from_abbrev(abbrev, day)
                if mmdd:
                    date_str = (self._add_year_to_date(mmdd, self.closing_date)
                                if self.closing_date else mmdd)
                    mid = mid_raw
            if date_str is None:
                # "Apr 15  Description  Category  Card  $Amount"
                m = _TXN_ABBREV.match(stripped)
                if m:
                    abbrev, day, mid_raw, amount_raw = m.groups()
                    mmdd = self._mmdd_from_abbrev(abbrev, day)
                    if mmdd:
                        date_str = (self._add_year_to_date(mmdd, self.closing_date)
                                    if self.closing_date else mmdd)
                        mid = mid_raw
            if date_str is None:
                # "04/15  Description  Category  Card  $Amount"
                m = _TXN_NUMERIC.match(stripped)
                if m:
                    date_raw, mid_raw, amount_raw = m.groups()
                    date_str = (self._add_year_to_date(date_raw, self.closing_date)
                                if self.closing_date else date_raw)
                    mid = mid_raw

            if date_str is None:
                continue

            amount = self._parse_amount(amount_raw)
            if amount is None:
                continue

            desc = re.sub(r'\s{2,}.*$', '', mid).strip()
            if not desc:
                continue

            txn_type = _classify_cc_transaction(desc, amount)
            if txn_type == 'payment':
                self.payments.append({
                    'date': date_str,
                    'description': desc,
                    'amount': abs(amount),
                })
            elif txn_type == 'credit' or amount < 0:
                self.credits.append({
                    'date': date_str,
                    'description': desc,
                    'amount': abs(amount),
                })
            else:
                vendor = self.normalize_vendor(desc)
                self.charges.append({
                    'date': date_str,
                    'vendor': vendor,
                    'amount': amount,
                })

    def generate_report(self):
        aggregated = self._aggregate_by_vendor(
            [{'date': c['date'], 'vendor': c['vendor'], 'amount': c['amount']}
             for c in self.charges
             if 'INTEREST' not in c['vendor'].upper()],
            date_fmt='%m/%d/%y',
        )
        total_charges = sum(r['amount'] for r in aggregated)
        total_payments = sum(p['amount'] for p in self.payments)
        total_credits = sum(c['amount'] for c in self.credits)

        statement_charges = None
        if self.new_balance is not None and self.previous_balance is not None:
            statement_charges = (
                self.new_balance - self.previous_balance
                + total_payments + total_credits
                - self.fees - self.interest
            )
        aggregated, total_charges = _add_missing_row(aggregated, total_charges, statement_charges)

        report = _report_header(
            self.statement_type, self.client_name,
            statement_date=self.closing_date,
        )
        report += _summary_block([
            ('Previous Balance',  self.previous_balance),
            ('Payments',          total_payments),
            ('Credits / Returns', total_credits if total_credits else None),
            ('Purchases',         total_charges),
            ('Finance Charges',   self.fees + self.interest if self.fees + self.interest else None),
            ('New Balance',       self.new_balance),
        ])
        if self.previous_balance is not None and self.new_balance is not None:
            calc = (self.previous_balance + total_charges + self.fees + self.interest
                    - total_payments - total_credits)
            ok = _is_balanced(calc, self.new_balance)
            report += _balance_check(ok, calc)

        if self.payments:
            report += _payments_section(self.payments, total_payments)
        if self.credits:
            report += _credits_section(self.credits, total_credits)
        report += _charges_section(aggregated, total_charges)
        return report


from parsers.registry import register  # noqa: E402
register("capital_one", "Capital One Business Credit Card", CapitalOneParser, is_credit_card=True)
