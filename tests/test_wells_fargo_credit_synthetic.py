"""
test_wells_fargo_credit_synthetic.py
--------------------------------------
Synthetic regression coverage for WellsFargoCreditCardParser, purpose-built
as a companion to tests/dump_report.py for the Extract/Classify/Report
pipeline refactor (see REFACTORING_ROADMAP.md's "Architecture Proposal").

The one real fixture (confirmed via tests/dump_report.py) exercises
payments, credits, and charges well, but has no PERIODIC *FINANCE CHARGE*
line — self.finance_charge stays 0, so that summary row and the
_add_missing_row reconciliation path never render. This file covers that
gap, plus isolates the classification this parser is unusual for: the
credit-vs-charge split is purely geometric (raw line length vs. a column
threshold, not vendor text), and normalize_vendor() is only ever called for
the "credit but not a payment" case — payments use a fixed literal
description, and charges are never normalized at parse time at all.

No real client data — client_name is left unset (None).
"""
import unittest
from decimal import Decimal

from parsers.wells_fargo import WellsFargoCreditCardParser

_TEXT = (
    "Statement Closing Date....... 12/15/25\n"
    "Previous Balance $1,000.00\n"
    "New Balance = $700.67\n"
    "Transaction Details\n"
    "PERIODIC *FINANCE CHARGE* ON PURCHASES 45.67\n"
    # Credit column (short line, under the 116-char default threshold):
    # payment keyword match -> fixed 'PAYMENT - THANK YOU' description.
    "12/01 12/01 REF001 ONLINE PAYMENT - THANK YOU 500.00\n"
    # Credit column: no payment keyword -> generic credit, normalize_vendor() applied.
    "12/02 12/02 REF002 CONTOSO CASH BACK CREDIT 45.00\n"
    # Charge column (padded past the threshold): matches the with-ref regex.
    "12/03 12/03 REF003 CONTOSO WIDGETS INC" + " " * 90 + "80.00\n"
    # Charge column: single-word description with no separate ref token ->
    # falls through to the without-ref regex (the with-ref regex can't
    # match a line with only one content word before the amount).
    "12/04 12/04 " + " " * 100 + "Interest 120.00\n"
)


def _d(x):
    return Decimal(str(x))


class WellsFargoCreditSyntheticPipelineTest(unittest.TestCase):
    def _parser(self):
        p = WellsFargoCreditCardParser.__new__(WellsFargoCreditCardParser)
        p.client_name = None
        p.previous_balance = None
        p.new_balance = None
        p.payments = []
        p.credits = []
        p.charges = []
        p.finance_charge = Decimal('0')
        p.statement_year = None
        p.text = _TEXT
        return p

    def test_finance_charge_line_captured(self):
        p = self._parser()
        p.parse()
        self.assertEqual(p.finance_charge, _d('45.67'))

    def test_finance_charge_appears_in_report(self):
        p = self._parser()
        p.parse()
        report = p.generate_report()
        self.assertIn('Finance Charges', report)
        self.assertIn('45.67', report)

    def test_payment_uses_fixed_description_not_normalize_vendor(self):
        p = self._parser()
        p.parse()
        self.assertEqual(len(p.payments), 1)
        self.assertEqual(p.payments[0]['description'], 'PAYMENT - THANK YOU')
        self.assertEqual(p.payments[0]['amount'], _d('500.00'))

    def test_generic_credit_gets_normalize_vendor_applied(self):
        p = self._parser()
        p.parse()
        self.assertEqual(len(p.credits), 1)
        # No client config -> normalize_vendor()'s only effect is .strip()
        # (see StatementParser.normalize_vendor's fallback), so this also
        # confirms the call happens at all, not just that text is unchanged.
        self.assertEqual(p.credits[0]['description'], 'CONTOSO CASH BACK CREDIT')
        self.assertEqual(p.credits[0]['amount'], _d('45.00'))

    def test_charges_stay_unnormalized_at_parse_time(self):
        p = self._parser()
        p.parse()
        vendors = {c['vendor'] for c in p.charges}
        self.assertIn('CONTOSO WIDGETS INC', vendors)
        self.assertIn('Interest', vendors)

    def test_charge_with_ref_regex_matches(self):
        p = self._parser()
        p.parse()
        matching = [c for c in p.charges if c['vendor'] == 'CONTOSO WIDGETS INC']
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]['amount'], _d('80.00'))
        self.assertEqual(matching[0]['date'], '12/03/25')

    def test_charge_without_ref_regex_fallback_matches(self):
        p = self._parser()
        p.parse()
        matching = [c for c in p.charges if c['vendor'] == 'Interest']
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]['amount'], _d('120.00'))

    def test_bucket_counts_exhaustive(self):
        p = self._parser()
        p.parse()
        self.assertEqual(len(p.payments), 1)
        self.assertEqual(len(p.credits), 1)
        self.assertEqual(len(p.charges), 2)

    def test_report_balances(self):
        p = self._parser()
        p.parse()
        report = p.generate_report()
        self.assertIn('Balance verification: PASSED', report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
