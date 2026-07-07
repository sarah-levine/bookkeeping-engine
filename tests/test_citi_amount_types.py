"""
test_citi_amount_types.py
----------------------------
Regression test for a real bug found via a full regression sweep against
every real fixture in the Drive test-fixtures folder: parsers/citi.py
stored amounts as `str(amount)` instead of Decimal in several places
(CitiCheckingParser.adp_transactions/credit_card_payments/charges,
CitiVisaCostcoParser.charges in parse(), and CitiVisaCostcoParser.charges
in load_from_dict()) — inconsistent with sibling fields in the very same
functions (self.checks/self.credits), which already stored Decimal
directly.

This crashed reconcile_comprehensive.py's "flag unrecognized CC payments"
check on every real Citi checking/savings statement with an autopay/
credit-card-payment line: `f"${pmt['amount']:,.2f}"` raises
`Unknown format code 'f' for object of type 'str'` on a str amount — caught
by a broad except and printed as "CC flag check failed", silently
swallowing the intended "ASK CLIENT" flag every time.

Parser is instantiated via __new__ (bypassing PDF extraction) and fed
synthetic text — no PDF fixture needed.
"""
import unittest
from decimal import Decimal

from parsers.citi import CitiCheckingParser, CitiVisaCostcoParser


def _checking_parser(text):
    p = CitiCheckingParser.__new__(CitiCheckingParser)
    p.client_name = None
    p.closing_date = None
    p.previous_balance = Decimal('0')
    p.new_balance = Decimal('0')
    p.payments = []
    p.credits = []
    p.charges = []
    p.adp_transactions = []
    p.credit_card_payments = []
    p.checks = []
    p.total_payments = Decimal('0')
    p.total_credits = Decimal('0')
    p.total_charges = Decimal('0')
    p.total_checks = Decimal('0')
    p.account_number = ''
    p.statement_date = ''
    p.text = text
    return p


class CitiCheckingAmountTypesTest(unittest.TestCase):
    def test_credit_card_payment_amount_is_decimal(self):
        p = _checking_parser(
            "05/05 ACH DEBIT 1,902.85 1,000.00\n"
            "AUTOPAY PAYMENT CREDIT CRD\n"
        )
        p.parse()
        self.assertEqual(len(p.credit_card_payments), 1)
        self.assertIsInstance(p.credit_card_payments[0]['amount'], Decimal)
        self.assertEqual(p.credit_card_payments[0]['amount'], Decimal('1902.85'))

    def test_adp_transaction_amount_is_decimal(self):
        p = _checking_parser(
            "05/06 ACH DEBIT 500.00 500.00\n"
            "ADP PAYROLL FEES\n"
        )
        p.parse()
        self.assertEqual(len(p.adp_transactions), 1)
        self.assertIsInstance(p.adp_transactions[0]['amount'], Decimal)

    def test_plain_charge_amount_is_decimal(self):
        p = _checking_parser(
            "05/07 ACH DEBIT 75.00 425.00\n"
            "Some Vendor Inc\n"
        )
        p.parse()
        self.assertEqual(len(p.charges), 1)
        self.assertIsInstance(p.charges[0]['amount'], Decimal)

    def test_cc_payment_amount_survives_reconcile_flag_check_formatting(self):
        # The actual crash site: reconcile_comprehensive.py's unrecognized-
        # CC-payment flag does f"${pmt['amount']:,.2f}" — must not raise.
        p = _checking_parser(
            "05/05 ACH DEBIT 1,902.85 1,000.00\n"
            "AUTOPAY PAYMENT CREDIT CRD\n"
        )
        p.parse()
        pmt = p.credit_card_payments[0]
        formatted = f"${pmt['amount']:,.2f}"  # must not raise
        self.assertEqual(formatted, "$1,902.85")


class CitiVisaCostcoAmountTypesTest(unittest.TestCase):
    def test_load_from_dict_charges_amount_is_decimal(self):
        p = CitiVisaCostcoParser.__new__(CitiVisaCostcoParser)
        p.client_name = None
        p.closing_date = None
        p.statement_date = None
        p.load_from_dict({
            "beginning_balance": "0.00",
            "ending_balance": "100.00",
            "payments": [],
            "credits": [],
            "charges": [{"date": "05/01/26", "vendor": "Some Vendor", "amount": "50.00"}],
        })
        self.assertIsInstance(p.charges[0]['amount'], Decimal)
        self.assertEqual(p.charges[0]['amount'], Decimal('50.00'))


if __name__ == "__main__":
    unittest.main(verbosity=2)
