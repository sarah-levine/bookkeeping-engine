"""
test_chase_synthetic.py
------------------------
Synthetic regression coverage for ChaseParser, purpose-built as a companion
to tests/dump_report.py for the Extract/Classify/Report pipeline refactor
(see REFACTORING_ROADMAP.md's "Architecture Proposal").

Chase has 4 real fixtures (better real coverage than Northern Trust's one),
covering both statement line formats and most classification paths — but
this file pins down isolated, controlled coverage of:
  - both line formats (two-date / Sapphire+United, and single-date / Ink)
  - a payment (keyword match)
  - a credit (keyword match, e.g. 'REFUND')
  - a negative-amount line with NO keyword match (falls through to 'credit'
    via _classify_cc_transaction's amount-sign-only rule — note the two-date
    format's regex has no optional minus sign, so this case can only be
    exercised via the single-date format)
  - a plain purchase charge
  - an interest-charge line item (vendor contains 'INTEREST'), to confirm
    generate_report()'s interest/purchase split still routes it correctly

No real client data — client_name is left unset (None), matching the
existing tests/test_chase_balance_check.py pattern.
"""
import unittest
from decimal import Decimal

from parsers.chase import ChaseParser

_TEXT = (
    "Opening/Closing Date 05/22/26 - 06/21/26\n"
    "Previous Balance $1,000.00\n"
    "New Balance $512.34\n"
    # Two-date format (Sapphire/United shape) — keyword-matched payment.
    # Note: this format's amount has no optional minus sign at all.
    "04/16 04/15 AUTOMATIC PAYMENT - THANK YOU 500.00\n"
    # Single-date format (Ink shape) — plain purchase, no keyword match.
    "  06/01  CONTOSO WIDGETS INC  40.00\n"
    # Single-date format — keyword-matched credit (positive amount).
    "  06/03  CONTOSO REFUND  25.00\n"
    # Single-date format — negative amount, no keyword match at all ->
    # falls through to 'credit' via the amount-sign-only rule.
    "  06/05  CONTOSO ADJUSTMENT  -15.00\n"
    # Single-date format — interest charge line item (no keyword match,
    # positive amount -> 'charge'; separated from purchases in generate_report
    # by vendor text containing 'INTEREST').
    "  06/10  INTEREST CHARGE ON PURCHASES  12.34\n"
)


def _d(x):
    return Decimal(str(x))


class ChaseSyntheticPipelineTest(unittest.TestCase):
    """Builds a parser via __new__ (bypassing PDF extraction), matching the
    pattern already used in tests/test_chase_balance_check.py."""

    def _parser(self):
        p = ChaseParser.__new__(ChaseParser)
        p.client_name = None
        p.previous_balance = Decimal('0')
        p.new_balance = Decimal('0')
        p.total_payments = Decimal('0')
        p.interest_charged = Decimal('0')
        p.payments = []
        p.credits = []
        p.charges = []
        p.closing_date = None
        p.text = _TEXT
        return p

    def test_closing_date_and_balances_extracted(self):
        p = self._parser()
        p.parse()
        self.assertEqual(p.closing_date, '06/21/26')
        self.assertEqual(p.previous_balance, _d('1000.00'))
        self.assertEqual(p.new_balance, _d('512.34'))

    def test_two_date_format_payment_classified(self):
        p = self._parser()
        p.parse()
        self.assertEqual(len(p.payments), 1)
        self.assertEqual(p.payments[0]['date'], '04/16/26')
        self.assertEqual(p.payments[0]['description'], 'PAYMENT - THANK YOU')
        self.assertEqual(p.payments[0]['amount'], _d('500.00'))
        self.assertEqual(p.total_payments, _d('500.00'))

    def test_single_date_format_plain_charge_classified(self):
        p = self._parser()
        p.parse()
        charge_vendors = {c['vendor']: c for c in p.charges}
        self.assertIn('CONTOSO WIDGETS INC', charge_vendors)
        self.assertEqual(charge_vendors['CONTOSO WIDGETS INC']['amount'], '40.00')
        self.assertEqual(charge_vendors['CONTOSO WIDGETS INC']['date'], '06/01/26')

    def test_keyword_matched_credit_classified(self):
        p = self._parser()
        p.parse()
        descriptions = {c['description']: c for c in p.credits}
        self.assertIn('CONTOSO REFUND', descriptions)
        self.assertEqual(descriptions['CONTOSO REFUND']['amount'], _d('25.00'))

    def test_negative_amount_no_keyword_falls_through_to_credit(self):
        p = self._parser()
        p.parse()
        descriptions = {c['description']: c for c in p.credits}
        self.assertIn('CONTOSO ADJUSTMENT', descriptions)
        self.assertEqual(descriptions['CONTOSO ADJUSTMENT']['amount'], _d('15.00'))
        self.assertNotIn('CONTOSO ADJUSTMENT', {c['vendor'] for c in p.charges})

    def test_interest_line_item_lands_in_charges_not_credits_or_payments(self):
        p = self._parser()
        p.parse()
        charge_vendors = {c['vendor'] for c in p.charges}
        self.assertIn('INTEREST CHARGE ON PURCHASES', charge_vendors)
        self.assertNotIn('INTEREST CHARGE ON PURCHASES', {c['description'] for c in p.credits})
        self.assertNotIn('INTEREST CHARGE ON PURCHASES', {c['description'] for c in p.payments})

    def test_bucket_counts_are_exhaustive(self):
        # 5 transaction lines total: 1 payment, 2 credits (keyword + sign
        # fallback), 2 charges (plain purchase + interest).
        p = self._parser()
        p.parse()
        self.assertEqual(len(p.payments), 1)
        self.assertEqual(len(p.credits), 2)
        self.assertEqual(len(p.charges), 2)

    def test_report_balances_and_splits_interest_from_purchases(self):
        p = self._parser()
        p.parse()
        report = p.generate_report()
        self.assertIn('Balance verification: PASSED', report)
        self.assertIn('FINANCE CHARGES', report)
        self.assertIn('CONTOSO WIDGETS INC', report)
        self.assertIn('INTEREST CHARGE ON PURCHASES', report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
