"""
test_citi_savings_synthetic.py
--------------------------------
Synthetic regression coverage for CitiSavingsParser, purpose-built as a
companion to tests/dump_report.py for the Extract/Classify/Report pipeline
refactor (see REFACTORING_ROADMAP.md's "Architecture Proposal").

The one real Citi Savings fixture (confirmed via tests/dump_report.py) has
exactly one transaction — an INTEREST credit — and zero withdrawals, zero
checks, and no combined-statement scoping exercised (it's a standalone
savings statement). This file covers what that real fixture can't:
  - a debit/withdrawal (ACH DEBIT)
  - a check (CHECK NO: line), including the synthesized "Check #<num>"
    vendor label
  - a second credit-type variant (DEPOSIT) beyond INTEREST
  - the combined-statement scoping wrinkle: a decoy pre-SAVINGS ACTIVITY
    "checking section" with its own (different) balance lines and a
    Statement Period line, followed by the real SAVINGS ACTIVITY section —
    proves the decoy balances/transactions are correctly excluded by
    position (never reach the scoped extraction) while Statement Period is
    still picked up from the unscoped full text.

No real client data — client_name is left unset (None).
"""
import unittest
from decimal import Decimal

from parsers.citi import CitiSavingsParser

_TEXT = (
    "Statement Period: May 1 - May 31, 2026\n"
    # Decoy "checking section" before SAVINGS ACTIVITY — must be excluded
    # from balance/transaction extraction purely by position (it's before
    # the scope marker), even though Statement Period above is still
    # picked up (searched unscoped, over the full text).
    "Beginning Balance: $999,999.99\n"
    "Ending Balance: $888,888.88\n"
    "05/02 ACH DEBIT DECOY CHECKING TRANSACTION 50,000.00 949,999.99\n"
    "  DECOY VENDOR NOT REAL\n"
    "SAVINGS ACTIVITY\n"
    "Beginning Balance: $10,000.00\n"
    "Ending Balance: $10,135.00\n"
    "05/05 DEPOSIT SOME DEPOSIT DESC 100.00 10,100.00\n"
    "  CONTOSO DEPOSIT SOURCE  EXTRA COLUMN\n"
    "05/10 ACH DEBIT SOME DEBIT DESC 50.00 10,050.00\n"
    "  CONTOSO DEBIT VENDOR  EXTRA COLUMN\n"
    "05/12 CHECK NO: 1042 25.00 10,025.00\n"
    "  CONTOSO CHECK PAYEE  EXTRA COLUMN\n"
    "05/29 INTEREST Interest Earned 110.00 10,135.00\n"
    "  Interest Earned\n"
)


def _d(x):
    return Decimal(str(x))


class CitiSavingsSyntheticPipelineTest(unittest.TestCase):
    """Builds a parser via __new__ (bypassing PDF extraction), matching the
    pattern used in tests/test_northern_trust_synthetic.py and
    tests/test_chase_synthetic.py."""

    def _parser(self):
        p = CitiSavingsParser.__new__(CitiSavingsParser)
        p.client_name = None
        p.statement_date = ''
        p.closing_date = None
        p.beginning_balance = Decimal('0')
        p.ending_balance = Decimal('0')
        p.deposits = []
        p.withdrawals = []
        p.total_deposits = Decimal('0')
        p.total_withdrawals = Decimal('0')
        p.text = _TEXT
        return p

    def test_statement_period_picked_up_from_unscoped_decoy_section(self):
        p = self._parser()
        p.parse()
        self.assertEqual(p.statement_date, 'May 1 - May 31, 2026')
        self.assertEqual(p.closing_date, '05/31/26')

    def test_decoy_checking_balances_excluded_by_scoping(self):
        p = self._parser()
        p.parse()
        self.assertEqual(p.beginning_balance, _d('10000.00'))
        self.assertEqual(p.ending_balance, _d('10135.00'))

    def test_decoy_checking_transaction_excluded_by_scoping(self):
        p = self._parser()
        p.parse()
        all_vendors = {w['vendor'] for w in p.withdrawals} | {d['vendor'] for d in p.deposits}
        self.assertNotIn('DECOY VENDOR NOT REAL', all_vendors)
        self.assertNotIn('DECOY CHECKING TRANSACTION', all_vendors)

    def test_deposit_credit_type_classified(self):
        p = self._parser()
        p.parse()
        deposit_vendors = {d['vendor']: d for d in p.deposits}
        self.assertIn('Contoso Deposit Source', deposit_vendors)
        self.assertEqual(deposit_vendors['Contoso Deposit Source']['amount'], _d('100.00'))
        self.assertEqual(deposit_vendors['Contoso Deposit Source']['date'], '05/05/26')

    def test_ach_debit_classified_as_withdrawal(self):
        p = self._parser()
        p.parse()
        withdrawal_vendors = {w['vendor']: w for w in p.withdrawals}
        self.assertIn('Contoso Debit Vendor', withdrawal_vendors)
        self.assertEqual(withdrawal_vendors['Contoso Debit Vendor']['amount'], _d('50.00'))

    def test_check_synthesizes_vendor_label_and_lands_in_withdrawals(self):
        p = self._parser()
        p.parse()
        withdrawal_vendors = {w['vendor']: w for w in p.withdrawals}
        self.assertIn('Check #1042', withdrawal_vendors)
        self.assertEqual(withdrawal_vendors['Check #1042']['amount'], _d('25.00'))
        # The check's own vendor lookahead line must NOT be used as the vendor.
        self.assertNotIn('CONTOSO CHECK PAYEE', withdrawal_vendors)

    def test_interest_credit_still_classified(self):
        p = self._parser()
        p.parse()
        deposit_vendors = {d['vendor']: d for d in p.deposits}
        self.assertIn('Interest Earned', deposit_vendors)
        self.assertEqual(deposit_vendors['Interest Earned']['amount'], _d('110.00'))

    def test_bucket_counts_and_totals(self):
        p = self._parser()
        p.parse()
        self.assertEqual(len(p.deposits), 2)
        self.assertEqual(len(p.withdrawals), 2)
        self.assertEqual(p.total_deposits, _d('210.00'))
        self.assertEqual(p.total_withdrawals, _d('75.00'))

    def test_report_balances(self):
        p = self._parser()
        p.parse()
        report = p.generate_report()
        self.assertIn('Balance verification: PASSED', report)
        self.assertIn('Check #1042', report)
        self.assertIn('Contoso Debit Vendor', report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
