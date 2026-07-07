"""
test_cc_payment_classification.py
------------------------------------
Regression coverage for _is_known_cc_network_payment() (parsers/base.py) and
its use in BankOfAmericaCheckingParser/AmexCheckingParser's debit
classification.

Previously each checking parser hand-rolled its own partial, mutually
inconsistent inline pattern list, and neither included bare major
card-network names ('AMERICAN EXPRESS', 'CAPITAL ONE', 'DISCOVER', 'BANK OF
AMERICA', 'WELLS FARGO') — the exact gap that let a real ~$3,900/mo Amex
bill payment silently land in generic "Withdrawals and Debits" instead of
"Credit Card Payments" (see REFACTORING_ROADMAP.md's cc_keywords item).

Parsers are instantiated via __new__ (bypassing PDF extraction) and fed
synthetic transactions, matching the pattern in tests/test_aggregations.py —
no PDF fixtures needed.
"""
import unittest
from decimal import Decimal

from parsers.base import _is_known_cc_network_payment
from parsers.bofa import BankOfAmericaCheckingParser
from parsers.amex import AmexCheckingParser


def _d(x):
    return Decimal(str(x))


class IsKnownCCNetworkPaymentTest(unittest.TestCase):
    def test_previously_supported_patterns_still_match(self):
        # These were already hardcoded (in one parser or the other) before
        # the consolidation — must keep matching.
        for desc in [
            'CREDIT CARD PAYMENT', 'AMEX EPAYMENT ACH', 'CHASE CREDIT CRD PAYMENT',
            'CITI CARD ONLINE PYMT', 'CITICTP AUTOPAY',
        ]:
            self.assertTrue(_is_known_cc_network_payment(desc), desc)

    def test_previously_missing_networks_now_match(self):
        # The actual gap that caused the real incident: bare network names
        # with no other qualifying keyword.
        for desc in [
            'AMERICAN EXPRESS ACH PMT', 'CAPITAL ONE ONLINE PAYMENT',
            'DISCOVER CARD PAYMENT', 'BANK OF AMERICA CREDIT CARD PMT',
            'WELLS FARGO CARD PAYMENT', 'BMO CREDIT CARD PAYMENT',
        ]:
            self.assertTrue(_is_known_cc_network_payment(desc), desc)

    def test_unrelated_vendor_not_misclassified(self):
        for desc in [
            'AMAZON.COM RETAIL', 'STAPLES OFFICE SUPPLIES', 'PGANDE WEB ONLINE',
            'PAYROLL ADP WAGE PAY', 'RENT CHECK 1001',
        ]:
            self.assertFalse(_is_known_cc_network_payment(desc), desc)


class BofaCheckingCCClassificationTest(unittest.TestCase):
    def _parser(self, debits):
        p = BankOfAmericaCheckingParser.__new__(BankOfAmericaCheckingParser)
        p.client_name = None  # no client config -> only the generic fallback applies
        p.credits = []
        p.debits = debits
        return p

    def test_amex_bill_payment_lands_in_credit_card_payments(self):
        p = self._parser(debits=[
            {'date': '06/05/26', 'vendor': 'AMERICAN EXPRESS ACH PMT', 'amount': _d(-3900)},
        ])
        _, all_debits, cc_payments, _, _, _ = p.aggregate_transactions()
        self.assertEqual(len(cc_payments), 1, "should be classified as a CC payment, not a plain debit")
        self.assertFalse(all_debits, "must not also land in generic Withdrawals and Debits")

    def test_generic_vendor_stays_a_plain_debit(self):
        p = self._parser(debits=[
            {'date': '06/05/26', 'vendor': 'STAPLES OFFICE SUPPLIES', 'amount': _d(-40)},
        ])
        _, all_debits, cc_payments, _, _, _ = p.aggregate_transactions()
        self.assertFalse(cc_payments)
        self.assertEqual(len(all_debits), 1)

    def test_credit_card_payments_exposed_on_self(self):
        # Regression: reconcile_comprehensive.py's "flag unrecognized CC
        # payments" check reads getattr(parser, 'credit_card_payments', [])
        # — before this fix, BofA's checking parser only ever returned this
        # list locally from aggregate_transactions(), never set it on self,
        # so the flag silently never fired for any BofA checking statement
        # (found 2026-07-07: this is exactly why a real $3,900 Amex payment
        # on a BofA checking statement was never auto-flagged).
        p = self._parser(debits=[
            {'date': '06/29/26', 'vendor': 'AMERICAN EXPRESS ACH PMT', 'amount': _d(-3900)},
        ])
        p.aggregate_transactions()
        self.assertEqual(len(p.credit_card_payments), 1)
        self.assertEqual(p.credit_card_payments[0]['vendor'], 'AMERICAN EXPRESS ACH PMT')


class AmexCheckingCCClassificationTest(unittest.TestCase):
    """AmexCheckingParser.aggregate_transactions() folds CC payments back
    into `withdrawals` (unlike BofA checking, which keeps a separate
    bucket) — but CC payments are never vendor-aggregated, so the
    observable difference is individual (unaggregated) line items vs. one
    summed line for an ordinary same-vendor debit."""

    def _parser(self, debits):
        p = AmexCheckingParser.__new__(AmexCheckingParser)
        p.client_name = None
        p.checks = []
        p.credits = []
        p.debits = debits
        return p

    def test_capital_one_payments_stay_individual_not_aggregated(self):
        p = self._parser(debits=[
            {'date': '06/05/26', 'description': 'CAPITAL ONE ONLINE PAYMENT', 'amount': _d(-500)},
            {'date': '06/20/26', 'description': 'CAPITAL ONE ONLINE PAYMENT', 'amount': _d(-600)},
        ])
        deposits, withdrawals, adp, checks = p.aggregate_transactions()
        matching = [w for w in withdrawals if 'CAPITAL ONE' in w['vendor'].upper()]
        self.assertEqual(len(matching), 2,
                         "CC payments must stay as individual lines, not a single aggregated line")

    def test_unclassified_vendor_still_gets_aggregated_normally(self):
        p = self._parser(debits=[
            {'date': '06/05/26', 'description': 'CONTOSO WIDGETS INC', 'amount': _d(-40)},
            {'date': '06/20/26', 'description': 'CONTOSO WIDGETS INC', 'amount': _d(-60)},
        ])
        deposits, withdrawals, adp, checks = p.aggregate_transactions()
        matching = [w for w in withdrawals if 'CONTOSO' in w['vendor'].upper()]
        self.assertEqual(len(matching), 1, "non-CC same-vendor debits should still aggregate to one line")
        self.assertEqual(matching[0]['count'], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
