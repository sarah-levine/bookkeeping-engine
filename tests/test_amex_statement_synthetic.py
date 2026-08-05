"""
test_amex_statement_synthetic.py
---------------------------------
Synthetic regression coverage for AmexStatementParser (the Amex Business
credit card class, distinct from AmexCheckingParser), purpose-built as a
companion to tests/dump_report.py for the Extract/Classify/Report pipeline
refactor (see REFACTORING_ROADMAP.md's "Architecture Proposal").

The 4 real fixtures (confirmed via tests/dump_report.py) exercise payments,
cardholder-prefixed and plain credits, cardholder-scoped charges, and fee/
interest totals well -- but don't confirm from visible report output alone:
the separate-line-amount fallback charge format (vs. the inline-amount
format), the fee-keyword skip list actually excluding a charges-section
line, the finance-charge dedup filter actually firing, or (since all 3 real
fixture clients happen to have cardholders configured) that extraction
still works correctly for a client with NO cardholders configured at all.

The synthetic text below was constructed and verified against the real
(pre-migration) parser directly, since the two-pass/cardholder-state/
fallback-format structure needs exact structural matching.

CLIENT_CARDHOLDERS is patched directly on parsers.amex (not via
tests._registry_test_utils.install_example_registry -- that only swaps
parsers.base._registry, but parsers.amex imports CLIENT_CARDHOLDERS as a
plain dict captured once at module-import time, so swapping the registry
singleton after the fact has no effect on it). The fictional client name
("ACME INC") and cardholder names ("JANE DOE" / "JOHN ROE") match the
repo's own clients/example_client.json placeholders -- no real client
data.
"""
import unittest
from unittest import mock
from decimal import Decimal

from parsers.amex import AmexStatementParser

CLIENT = "ACME INC"
_CARDHOLDERS = {CLIENT: ["JANE DOE", "JOHN ROE"]}

_TEXT = (
    "Closing Date 01/31/26\n"
    "Account Ending 1-91004\n"
    "Previous Balance   $1,000.00\n"
    "\n"
    "Payments\n"
    "01/05/26   AUTOPAY PAYMENT RECEIVED - THANK YOU          -$500.00\n"
    "01/06/26 JANE DOE MERCHANT REFUND CREDIT   -$25.00\n"
    "01/07/26 CONTOSO REVERSAL ADJUSTMENT   -$10.00\n"
    "\n"
    "New Charges\n"
    "JANE DOE\n"
    "01/10/26   Contoso Vendor Supplies   $150.00\n"
    # Negative amount inside the New Charges section routes to self.credits
    # via the charges pass. Previously ALSO matched independently by the
    # unscoped Payments/Credits pass, double-counting the same credit --
    # fixed 2026-07-14 by bounding that pass to charges_start (see
    # REFACTORING_ROADMAP.md's "Closed: Fixed").
    "01/11/26   Contoso Refund Adjustment   -$10.00\n"
    "01/12/26   Annual Fee   $95.00\n"
    # Separate-line-amount fallback format: date+vendor on one line, the
    # amount alone on the next line (vs. the inline "vendor ... $amount"
    # format used above).
    "01/13/26   Contoso Widget Co\n"
    "$85.00\n"
    # Amount equals Total Fees + Total Interest below ($20.00) and the
    # vendor text contains "Finance" -- the post-loop dedup filter must
    # remove this from self.charges (it's already tallied in Finance
    # Charges, so keeping it would double-count).
    "01/14/26   Periodic Finance Charge   $20.00\n"
    "Total Fees for this Period   $15.00\n"
    "Total Interest Charged for this Period   $5.00\n"
    "New Balance   $945.00\n"
)

# None of the 4 real fixtures exercise a client with NO cardholders
# configured (all 3 real fixture clients happen to be multi-cardholder Amex
# accounts) -- this confirms the never-matching cardholder_pattern /
# _cardholder_inner='(?!)' fallback still extracts correctly on its own.
_NO_CARDHOLDER_TEXT = (
    "Closing Date 02/28/26\n"
    "Account Ending 291105\n"
    "Previous Balance   $200.00\n"
    "New Charges\n"
    "02/05/26   Contoso Plain Vendor   $60.00\n"
    "New Balance   $260.00\n"
)


def _d(x):
    return Decimal(str(x))


class AmexStatementSyntheticPipelineTest(unittest.TestCase):
    def setUp(self):
        self._patcher = mock.patch.dict('parsers.amex.CLIENT_CARDHOLDERS', _CARDHOLDERS, clear=False)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def _parser(self, text=_TEXT, client_name=CLIENT):
        p = AmexStatementParser.__new__(AmexStatementParser)
        p.client_name = client_name
        p.closing_date = None
        p.account_number = None
        p.previous_balance = None
        p.new_balance = None
        p.payments = []
        p.credits = []
        p.charges = []
        p.fees = Decimal('0')
        p.interest = Decimal('0')
        p.text = text
        return p

    def test_metadata_extracted(self):
        p = self._parser()
        p.parse()
        self.assertEqual(p.closing_date, '01/31/26')
        self.assertEqual(p.account_number, '1-91004')
        self.assertEqual(p.previous_balance, _d('1000.00'))
        self.assertEqual(p.new_balance, _d('945.00'))

    def test_payment_captured(self):
        p = self._parser()
        p.parse()
        self.assertEqual(len(p.payments), 1)
        self.assertEqual(p.payments[0]['description'], 'AUTOPAY PAYMENT RECEIVED - THANK YOU')
        self.assertEqual(p.payments[0]['amount'], _d('500.00'))

    def test_cardholder_prefixed_credit_from_payments_pass(self):
        p = self._parser()
        p.parse()
        descs = {c['description']: c for c in p.credits}
        self.assertIn('MERCHANT REFUND CREDIT', descs)
        self.assertEqual(descs['MERCHANT REFUND CREDIT']['amount'], _d('25.00'))

    def test_plain_credit_from_payments_pass(self):
        p = self._parser()
        p.parse()
        descs = {c['description'] for c in p.credits}
        self.assertIn('CONTOSO REVERSAL ADJUSTMENT', descs)

    def test_inline_charge_with_cardholder(self):
        p = self._parser()
        p.parse()
        by_vendor = {c['vendor']: c for c in p.charges}
        self.assertIn('Contoso Vendor Supplies', by_vendor)
        self.assertEqual(by_vendor['Contoso Vendor Supplies']['cardholder'], 'JANE DOE')
        self.assertEqual(by_vendor['Contoso Vendor Supplies']['amount'], _d('150.00'))

    def test_separate_line_amount_fallback_charge(self):
        p = self._parser()
        p.parse()
        by_vendor = {c['vendor']: c for c in p.charges}
        self.assertIn('Contoso Widget Co', by_vendor)
        self.assertEqual(by_vendor['Contoso Widget Co']['amount'], _d('85.00'))
        # current_cardholder persists from the standalone header line seen
        # earlier in the section -- no new header line precedes this charge.
        self.assertEqual(by_vendor['Contoso Widget Co']['cardholder'], 'JANE DOE')

    def test_negative_amount_in_charges_section_routes_to_credit_not_charge(self):
        p = self._parser()
        p.parse()
        self.assertFalse(any(c['vendor'] == 'Contoso Refund Adjustment' for c in p.charges))
        matching = [c for c in p.credits if c['description'] == 'Contoso Refund Adjustment']
        # Fixed 2026-07-14: the Payments/Credits pass now stops before the
        # Charges section, so this line is matched exactly once (previously
        # double-counted -- see REFACTORING_ROADMAP.md's "Closed: Fixed").
        self.assertEqual(len(matching), 1)

    def test_fee_keyword_charge_excluded(self):
        p = self._parser()
        p.parse()
        self.assertFalse(any('Annual Fee' in c['vendor'] for c in p.charges))
        self.assertFalse(any('Annual Fee' in c['description'] for c in p.credits))

    def test_finance_charge_dedup_filter_removes_matching_charge(self):
        p = self._parser()
        p.parse()
        self.assertEqual(p.fees, _d('15.00'))
        self.assertEqual(p.interest, _d('5.00'))
        self.assertFalse(any('Finance Charge' in c['vendor'] for c in p.charges))

    def test_bucket_counts_exhaustive(self):
        p = self._parser()
        p.parse()
        self.assertEqual(len(p.payments), 1)
        self.assertEqual(len(p.credits), 3)
        self.assertEqual(len(p.charges), 2)

    def test_report_balances(self):
        p = self._parser()
        p.parse()
        report = p.generate_report()
        self.assertIn('Balance verification: PASSED', report)

    def test_no_cardholder_config_still_extracts_charges(self):
        p = self._parser(text=_NO_CARDHOLDER_TEXT, client_name=None)
        p.parse()
        self.assertEqual(len(p.charges), 1)
        self.assertEqual(p.charges[0]['vendor'], 'Contoso Plain Vendor')
        self.assertEqual(p.charges[0]['cardholder'], '')
        report = p.generate_report()
        self.assertIn('Balance verification: PASSED', report)

    def test_metadata_extracted_with_multi_space_labels(self):
        # pdftotext -layout column alignment can insert many spaces between
        # words in a label ("New    balance"); contains_label() must still
        # match. Regression test for REFACTORING_ROADMAP.md's "Literal
        # single-space label gates across every bank parser".
        text = _TEXT.replace(
            "Closing Date 01/31/26", "Closing    Date 01/31/26"
        ).replace(
            "Account Ending 1-91004", "Account     Ending 1-91004"
        )
        p = self._parser(text=text)
        p.parse()
        self.assertEqual(p.closing_date, '01/31/26')
        self.assertEqual(p.account_number, '1-91004')


if __name__ == "__main__":
    unittest.main(verbosity=2)
