"""Config-driven transaction roll-ups (parsers.*.aggregate_transactions).

Locks in that vendor roll-ups (formerly a hardcoded special case) are now driven
entirely by a client's `transaction_aggregations` config — no brand/vendor names
live in parser code, so the parsers stay public-repo safe.

Parsers are instantiated via __new__ (bypassing PDF extraction) and fed
synthetic transactions, so these tests need no PDF fixtures / pdfplumber.
"""
import pathlib
import unittest
from decimal import Decimal

from parsers.amex import AmexCheckingParser
from parsers.bofa import BankOfAmericaCheckingParser

# example_client.json ("ACME INC") configures:
#   transaction_aggregations: [{match: "ACME SPORTS", label: "Acme Sports",
#                               card_label: "Acme Sports (PROGRAM FEES)"}]
CLIENT = "ACME INC"


def _d(x):
    return Decimal(str(x))


class AmexAggregation(unittest.TestCase):
    def _parser(self, credits, debits):
        p = AmexCheckingParser.__new__(AmexCheckingParser)
        p.client_name = CLIENT
        p.checks = []
        p.credits = credits
        p.debits = debits
        return p

    def test_configured_vendor_rolled_up_with_card_label(self):
        p = self._parser(
            credits=[
                {'date': '01/05/26', 'description': 'ACME SPORTS TRANSFER 111', 'amount': _d(100)},
                {'date': '01/20/26', 'description': 'ACME SPORTS TRANSFER 222', 'amount': _d(50)},
                {'date': '01/10/26', 'description': 'PLAIN VENDOR', 'amount': _d(25)},
            ],
            debits=[
                {'date': '01/07/26', 'description': 'ACME SPORTS FEE 333', 'amount': _d(-40)},
            ],
        )
        deposits, withdrawals, adp, checks = p.aggregate_transactions()

        rolled = [d for d in deposits if d['vendor'] == 'Acme Sports (PROGRAM FEES)']
        self.assertEqual(len(rolled), 1)
        self.assertEqual(rolled[0]['amount'], _d(150))
        self.assertEqual(rolled[0]['count'], 2)
        self.assertEqual(rolled[0]['date'], '01/20/26')  # latest date
        # the unrelated vendor is NOT folded into the roll-up
        self.assertTrue(any(d['vendor'] != 'Acme Sports (PROGRAM FEES)' for d in deposits))

        rolled_w = [w for w in withdrawals if w['vendor'] == 'Acme Sports (PROGRAM FEES)']
        self.assertEqual(len(rolled_w), 1)
        self.assertEqual(rolled_w[0]['amount'], _d(-40))

    def test_no_config_means_no_rollup(self):
        # A client with no transaction_aggregations config: matching txns flow
        # through as ordinary (normalized) vendors, never rolled up.
        p = self._parser(
            credits=[{'date': '01/05/26', 'description': 'ACME SPORTS TRANSFER 111', 'amount': _d(100)}],
            debits=[],
        )
        p.client_name = None  # get_config(None) -> no rules
        deposits, _, _, _ = p.aggregate_transactions()
        self.assertFalse(any('PROGRAM FEES' in d['vendor'] for d in deposits))


class BofaCheckingAggregation(unittest.TestCase):
    def _parser(self, credits, debits):
        p = BankOfAmericaCheckingParser.__new__(BankOfAmericaCheckingParser)
        p.client_name = CLIENT
        p.credits = credits
        p.debits = debits
        return p

    def test_configured_vendor_uses_plain_label(self):
        # BofA checking uses `label` (not `card_label`); credits roll into the
        # deposits list, debits into the dedicated 5th return value.
        p = self._parser(
            credits=[
                {'date': '02/03/26', 'vendor': 'ACME SPORTS DEPOSIT 1', 'amount': _d(200)},
                {'date': '02/18/26', 'vendor': 'ACME SPORTS DEPOSIT 2', 'amount': _d(300)},
                {'date': '02/05/26', 'vendor': 'PLAIN VENDOR', 'amount': _d(10)},
            ],
            debits=[
                {'date': '02/09/26', 'vendor': 'ACME SPORTS FEE A', 'amount': _d(-15)},
                {'date': '02/11/26', 'vendor': 'ACME SPORTS FEE B', 'amount': _d(-25)},
            ],
        )
        credits, all_debits, cc, adp, special_debits, transfers = p.aggregate_transactions()

        rolled = [c for c in credits if c['vendor'] == 'Acme Sports']
        self.assertEqual(len(rolled), 1)
        self.assertEqual(rolled[0]['amount'], _d(500))
        self.assertEqual(rolled[0]['count'], 2)

        self.assertEqual(len(special_debits), 1)
        self.assertEqual(special_debits[0]['vendor'], 'Acme Sports')
        self.assertEqual(special_debits[0]['amount'], _d(-40))
        self.assertEqual(special_debits[0]['count'], 2)


class ConfigDrivenAggregation(unittest.TestCase):
    """Regression guard: aggregation must stay config-driven. The parsers read
    roll-up rules from `transaction_aggregations` (client config) and hold no
    specific vendor/brand literal of their own."""

    def test_parsers_read_aggregations_from_config(self):
        root = pathlib.Path(__file__).resolve().parent.parent / 'parsers'
        for f in ('amex.py', 'bofa.py'):
            text = (root / f).read_text()
            self.assertIn('transaction_aggregations', text,
                          f"parsers/{f} should source roll-ups from client config")


if __name__ == '__main__':
    unittest.main()
