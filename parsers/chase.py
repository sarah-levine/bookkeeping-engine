import sys
import re
import os
import json
from pathlib import Path
from decimal import Decimal
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from parsers.base import StatementParser, _registry, KNOWN_CLIENTS, _classify_cc_transaction
from parsers.row_schema import TransactionRow
from parsers.report import *
from parsers.report import (
    _report_header, _summary_block, _balance_check, _is_balanced,
    _payments_section, _credits_section, _individual_section, _deposits_section,
    _checks_section, _adp_section, _cc_payments_section, _add_missing_row,
    _charges_section, _safe_date_key, _now_pst
)

class ChaseParser(StatementParser):
    """
    Chase Business Credit Cards.
    Handles all Chase card formats: Ink, United, Sapphire, and others.

    Statement formats supported:
      - MM/DD  DESCRIPTION  AMOUNT          (single date, e.g. Chase Ink)
      - MM/DD  MM/DD  DESCRIPTION  AMOUNT   (two dates, e.g. Chase United/Sapphire)
    Card name is auto-detected from statement text.
    """

    # Default — overridden at parse time based on statement text
    statement_type = "Chase Business Credit Card"

    # Map of keywords to statement type labels
    _CARD_NAMES = {
        'INK':      'Chase Ink Business Credit Card',
        'UNITED':   'Chase United Credit Card',
        'SAPPHIRE': 'Chase Sapphire Preferred',
        'FREEDOM':  'Chase Freedom Business',
    }

    def __init__(self, pdf_path, client_name=None):
        super().__init__(pdf_path, client_name)
        self.previous_balance = Decimal('0')
        self.new_balance = Decimal('0')
        self.total_payments = Decimal('0')
        self.interest_charged = Decimal('0')
        self.payments = []
        self.credits = []
        self.charges = []
        self.closing_date = None

    def parse(self):
        lines = self.text.split('\n')

        # Auto-detect card name from statement text
        sample = self.text[:2000].upper()
        for keyword, label in self._CARD_NAMES.items():
            if keyword in sample:
                self.statement_type = label
                break

        rows = self._extract_rows(lines)
        self._rows_to_legacy_shape(rows)

    def _classify_row(self, date_str, vendor, raw_description, amount):
        """Classify one raw (date, vendor, amount) into a TransactionRow.

        Unlike Northern Trust's statement text (which literally prints
        "ACH Debit"/"ACH Credit" labels — real extraction-time signal),
        Chase's lines carry no type label at all: classifying a line as
        payment/credit/charge requires _classify_cc_transaction()
        (parsers/base.py, already the shared Classify building block used
        by other credit-card parsers), which needs vendor text *and* amount
        together. There's no raw-text-only signal to defer this with, so
        classification happens here, inside extraction — documented as a
        Chase-specific wrinkle, not a violation of the Extract/Classify
        boundary in principle.
        """
        txn_type = _classify_cc_transaction(vendor, amount)
        if txn_type == 'payment':
            row_type, signed_amount = 'payment', abs(amount)
        elif txn_type == 'credit':
            row_type, signed_amount = 'credit', abs(amount)
        else:  # 'charge' -> schema's 'debit'; _classify_cc_transaction
            # guarantees amount >= 0 whenever it returns 'charge' (every
            # negative-amount case is routed to 'payment' or 'credit'
            # earlier), so this abs() is a no-op today, kept for safety.
            row_type, signed_amount = 'debit', -abs(amount)
        return TransactionRow(
            date=date_str, vendor=vendor, raw_description=raw_description,
            amount=signed_amount, type=row_type,
        )

    def _extract_rows(self, lines):
        """Extract stage (see parsers/row_schema.py): raw statement text ->
        list[TransactionRow]. Statement metadata (card name, closing date,
        balances, interest_charged) and transaction-row extraction stay
        interleaved in one pass over `lines`, exactly as before this
        migration — closing_date must already be known at the point a given
        transaction line is processed for _add_year_to_date() to apply, and
        that depends on where "Opening/Closing Date" happens to fall in the
        statement text relative to the transaction lines. Splitting this
        into two separate passes (metadata first, then transactions) would
        silently change behavior for any statement where a transaction line
        precedes the closing-date line."""
        rows = []

        for line in lines:
            # Statement closing date
            if 'Opening/Closing Date' in line and not self.closing_date:
                m = re.search(r'(\d{2}/\d{2}/\d{2})\s*-\s*(\d{2}/\d{2}/\d{2})', line)
                if m:
                    self.closing_date = m.group(2)
            if 'Statement Date' in line and not self.closing_date:
                m = re.search(r'(\d{2}/\d{2}/\d{2})', line)
                if m:
                    self.closing_date = m.group(1)

            # Balances
            if 'Previous Balance' in line and not self.previous_balance:
                m = re.search(r'\$?([\d,]+\.\d{2})', line)
                if m:
                    self.previous_balance = Decimal(m.group(1).replace(',', ''))
            if 'New Balance' in line and 'Minimum' not in line and not self.new_balance:
                m = re.search(r'\$?([\d,]+\.\d{2})', line)
                if m:
                    self.new_balance = Decimal(m.group(1).replace(',', ''))
            if 'Interest Charged' in line and not self.interest_charged:
                m = re.search(r'\+?\$?([\d,]+\.\d{2})', line)
                if m:
                    self.interest_charged = Decimal(m.group(1).replace(',', ''))

            # Two-date format: MM/DD  MM/DD  DESCRIPTION  AMOUNT
            m = re.match(
                r'(\d{2}/\d{2})\s+(\d{2}/\d{2})\s+(.+?)\s+([\d,]+\.\d{2})\s*$', line)
            if m:
                post_date, _trans_date, description, amount_str = m.groups()
                if self.closing_date:
                    post_date = self._add_year_to_date(post_date, self.closing_date)
                vendor = re.sub(r'\s+', ' ', description.strip())
                amount = Decimal(amount_str.replace(',', ''))
                rows.append(self._classify_row(post_date, vendor, description, amount))
                continue

            # Single-date format: MM/DD  DESCRIPTION  AMOUNT
            m = re.match(
                r'\s*(\d{2}/\d{2})\s{2,}(.+?)\s{2,}(-?[\d,]*\.\d{2})\s*$', line)
            if not m:
                continue
            date_str = m.group(1)
            if self.closing_date:
                date_str = self._add_year_to_date(date_str, self.closing_date)
            vendor = re.sub(r'\s+', ' ', m.group(2).strip())
            amount_str = m.group(3).replace(',', '')
            amount = Decimal(amount_str)
            rows.append(self._classify_row(date_str, vendor, m.group(2), amount))

        return rows

    def _rows_to_legacy_shape(self, rows):
        """Adapter: list[TransactionRow] -> self.payments/self.credits/
        self.charges in their existing dict shapes. Note the pre-existing
        field-name inconsistency this preserves exactly: payments/credits
        use the key 'description', charges use 'vendor' (parsers/report.py's
        own convention, not something this migration changes). Payments
        always display the fixed literal 'PAYMENT - THANK YOU' regardless
        of raw vendor text. Charges' amount is stored as str(Decimal), not
        Decimal, matching the parser's pre-existing (if unusual) convention —
        _aggregate_by_vendor() tolerates either via its own Decimal(str(...))
        cast, but this preserves the exact prior shape rather than relying
        on that tolerance."""
        for row in rows:
            if row.type == 'payment':
                self.payments.append({'date': row.date, 'description': 'PAYMENT - THANK YOU', 'amount': row.amount})
                self.total_payments += row.amount
            elif row.type == 'credit':
                self.credits.append({'date': row.date, 'description': row.vendor, 'amount': row.amount})
            else:  # 'debit' -> charge
                self.charges.append({'date': row.date, 'vendor': row.vendor, 'amount': str(abs(row.amount))})

    def generate_report(self):
        # Separate interest rows from purchase charges
        interest_rows = [c for c in self.charges if 'INTEREST' in c['vendor'].upper()]
        purchase_rows = [c for c in self.charges if 'INTEREST' not in c['vendor'].upper()]

        aggregated_purchases = self._aggregate_by_vendor(purchase_rows, date_fmt='%m/%d/%y')
        aggregated_interest = self._aggregate_by_vendor(interest_rows, date_fmt='%m/%d/%y')
        total_purchases = sum(r['amount'] for r in aggregated_purchases)
        total_interest = self.interest_charged or sum(r['amount'] for r in aggregated_interest)
        total_credits = sum(c['amount'] for c in self.credits)

        # Normalize credit descriptions
        normalized_credits = [
            dict(c, description=self.normalize_vendor(c['description']))
            for c in self.credits
        ]

        report = _report_header(self.statement_type, self.client_name,
                                statement_date=self.closing_date)
        report += _summary_block([
            ('Previous Balance',  self.previous_balance),
            ('Payments',          self.total_payments),
            ('Credits / Returns', total_credits if total_credits else None),
            ('Purchases',         total_purchases if total_purchases else None),
            ('Finance Charges',   total_interest if total_interest else None),
            ('New Balance',       self.new_balance),
        ])

        if self.previous_balance is not None and self.new_balance is not None:
            calc = (self.previous_balance + total_purchases + total_interest
                    - self.total_payments - total_credits)
            ok = _is_balanced(calc, self.new_balance)
            report += _balance_check(ok, calc)

        if self.payments:
            report += _payments_section(self.payments, self.total_payments)
        if normalized_credits:
            report += _credits_section(normalized_credits, total_credits)
        if aggregated_purchases:
            report += _charges_section(aggregated_purchases, total_purchases)
        if aggregated_interest:
            report += _charges_section(aggregated_interest, total_interest, title='FINANCE CHARGES')
        return report


# Aliases — all Chase credit card types share one parser
ChaseInkParser      = ChaseParser
ChaseUnitedParser   = ChaseParser
ChaseSapphireParser = ChaseParser

from parsers.registry import register  # noqa: E402
register("chase_ink", "Chase Ink Business Credit Card", ChaseParser, is_credit_card=True)
register("chase_sapphire", "Chase Sapphire Credit Card", ChaseParser, is_credit_card=True)
register("chase_united", "Chase United Credit Card", ChaseParser, is_credit_card=True)


