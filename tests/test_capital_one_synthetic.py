"""
test_capital_one_synthetic.py
-------------------------------
Regression coverage for CapitalOneParser, built from the exact structural
quirks found reconciling a real client's real Capital One Spark statement --
the first one this parser had ever actually seen (see
REFACTORING_ROADMAP.md's "Capital One -- no real fixture exists" item).
That first real run produced badly wrong numbers: New Balance off by
$22.35, Finance Charges showing a nonzero value that should have been
$0.00, and garbage transaction rows -- four separate bugs, all from the
same root cause (pdftotext -layout merging unrelated two-column page
content onto one output line, or splitting one transaction's two date
columns across what the regex assumed was a single date + description).

Uses fictional, self-contained data reproducing the same line shapes (see
CLAUDE.md's public-repo-hygiene rule) -- no real client data.
"""
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import parsers.base as _base_mod
import parsers.capital_one as _cap1_mod
from parsers.base import ClientRegistry
from parsers.capital_one import CapitalOneParser

CANONICAL = "BRAVO STUDIO LLC"

_FICTIONAL_CLIENT_CONFIG = {
    "client_name": "Bravo Studio LLC",
    "canonical_name": CANONICAL,
    "aliases": ["Bravo Studio"],
    "statement_types": ["capital_one"],
}

# Reproduces the real statement's exact shape:
# - No "Statement Ending ..." text anywhere -- only a billing-cycle range.
# - The Account Summary's "New Balance" line shares a text row with
#   unrelated left-column warning text that also contains a dollar amount
#   (the real statement: "...may have to pay a $39.00 late fee...  New
#   Balance  = $61.35" -- one merged pdftotext -layout line).
# - Same merge shape for "Fees Charged".
# - Transaction rows carry TWO dates (Trans Date, Post Date) before the
#   description and amount.
# - One payment row's negative amount has a space between "-" and "$".
_TEXT = (
    "Apr 08, 2026 - May 08, 2026 | 31 days in Billing Cycle\n"
    "Previous Balance                                    $22.35\n"
    "you may have to pay a $39.00 late fee                    New Balance          = $61.35\n"
    "$61.35                       $15.00                      Fees Charged           + $0.00\n"
    "Interest Charged                                          + $0.00\n"
    "\n"
    "BRAVO STUDIO LLC #1234: Transactions\n"
    "Trans Date      Post Date          Description                              Amount\n"
    "Apr 10          Apr 10             CONTOSO ONLINE PYMT                    - $22.35\n"
    "Apr 10          Apr 11             CONTOSO WIDGETS INC800-000-0000NY       $39.00\n"
    "May 02          May 02             CONTOSO CLOUD SVC                        $2.35\n"
    "May 04          May 04             CONTOSO SUBSCRIPTION                    $20.00\n"
)


def _d(x):
    return Decimal(str(x))


class CapitalOneSyntheticTest(unittest.TestCase):
    """Builds a parser via __new__ (bypassing PDF extraction), matching the
    pattern used in tests/test_northern_trust_synthetic.py."""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        clients_dir = Path(cls._tmpdir.name)
        (clients_dir / "bravo_studio.json").write_text(json.dumps(_FICTIONAL_CLIENT_CONFIG))
        cls._previous_base_registry = _base_mod._registry
        cls._previous_cap1_registry = _cap1_mod._registry
        new_registry = ClientRegistry(clients_dir=str(clients_dir))
        _base_mod._registry = new_registry
        _cap1_mod._registry = new_registry

    @classmethod
    def tearDownClass(cls):
        _base_mod._registry = cls._previous_base_registry
        _cap1_mod._registry = cls._previous_cap1_registry
        cls._tmpdir.cleanup()

    def _parser(self):
        p = CapitalOneParser.__new__(CapitalOneParser)
        p.client_name = "Bravo Studio LLC"
        p.closing_date = None
        p.previous_balance = None
        p.new_balance = None
        p.payments = []
        p.credits = []
        p.charges = []
        p.fees = Decimal('0')
        p.interest = Decimal('0')
        p.text = _TEXT
        return p

    def test_closing_date_from_billing_cycle_range_when_no_statement_ending_text(self):
        p = self._parser()
        p.parse()
        self.assertEqual(p.closing_date, '05/08/26')

    def test_new_balance_not_hijacked_by_unrelated_merged_column_text(self):
        p = self._parser()
        p.parse()
        self.assertEqual(p.new_balance, _d('61.35'))

    def test_fees_not_hijacked_by_unrelated_merged_column_text(self):
        p = self._parser()
        p.parse()
        self.assertEqual(p.fees, _d('0'))

    def test_previous_balance_still_correct(self):
        p = self._parser()
        p.parse()
        self.assertEqual(p.previous_balance, _d('22.35'))

    def test_two_date_transaction_row_uses_trans_date_and_full_description(self):
        p = self._parser()
        p.parse()
        vendors = {c['vendor'] for c in p.charges}
        self.assertIn('Contoso Widgets Inc800-000-0000ny', vendors)
        # Not misparsed as the post-date fragment "Apr 11"
        self.assertNotIn('Apr 11', vendors)
        amounts = {c['vendor']: c['amount'] for c in p.charges}
        self.assertEqual(amounts['Contoso Widgets Inc800-000-0000ny'], _d('39.00'))

    def test_space_separated_negative_sign_parsed_as_payment(self):
        p = self._parser()
        p.parse()
        self.assertEqual(len(p.payments), 1)
        self.assertEqual(p.payments[0]['description'], 'CONTOSO ONLINE PYMT')
        self.assertEqual(p.payments[0]['amount'], _d('22.35'))

    def test_balance_ties_end_to_end(self):
        p = self._parser()
        p.parse()
        report = p.generate_report()
        self.assertIn('PASSED', report)
        self.assertNotIn('MISSING', report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
