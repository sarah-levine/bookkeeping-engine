"""
test_chase_balance_check.py
------------------------------
Regression test for ChaseParser.generate_report() — found via a full
regression sweep against every real fixture in the Drive test-fixtures
folder: unlike every other credit-card parser (BofA, Amex, Wells Fargo,
Citi, Capital One), ChaseParser imported _balance_check but never called
it, so Chase Ink/Sapphire/United statements never printed an explicit
"Balance verification: PASSED/FAILED" line at all — a missing confirmation
step, not a silent miscalculation (the underlying numbers already tied out
correctly; verified against a real Chase Ink statement).

Parser is instantiated via __new__ (bypassing PDF extraction) and fed
synthetic data — no PDF fixture needed.
"""
import unittest
from decimal import Decimal as D

from parsers.chase import ChaseParser


def _parser(previous_balance, new_balance, total_payments, charges=None,
            credits=None, interest_charged=D('0')):
    p = ChaseParser.__new__(ChaseParser)
    p.client_name = None
    p.closing_date = '06/05/26'
    p.previous_balance = previous_balance
    p.new_balance = new_balance
    p.total_payments = total_payments
    p.interest_charged = interest_charged
    p.payments = []
    p.credits = credits or []
    p.charges = charges or []
    return p


class ChaseBalanceCheckTest(unittest.TestCase):
    def test_balanced_statement_reports_passed(self):
        # previous(10207.57) + purchases(11941.04) - payments(9700.14) - credits(507.43) = new(11941.04)
        p = _parser(
            previous_balance=D('10207.57'),
            new_balance=D('11941.04'),
            total_payments=D('9700.14'),
            credits=[{'date': '04/03/26', 'description': 'Refund', 'amount': D('507.43')}],
            charges=[{'date': '04/01/26', 'vendor': 'Some Vendor', 'amount': D('11941.04')}],
        )
        report = p.generate_report()
        self.assertIn('Balance verification', report, "must print a balance verification line at all")
        self.assertIn('PASSED', report)
        self.assertNotIn('FAILED', report)

    def test_unbalanced_statement_reports_failed(self):
        p = _parser(
            previous_balance=D('10207.57'),
            new_balance=D('11941.04'),
            total_payments=D('9700.14'),
            credits=[{'date': '04/03/26', 'description': 'Refund', 'amount': D('507.43')}],
            charges=[{'date': '04/01/26', 'vendor': 'Some Vendor', 'amount': D('999.00')}],  # wrong
        )
        report = p.generate_report()
        self.assertIn('Balance verification', report)
        self.assertIn('FAILED', report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
