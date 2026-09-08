"""
test_bofa_checking_synthetic.py
---------------------------------
Synthetic regression coverage for BankOfAmericaCheckingParser (and, via
inheritance, BankOfAmericaSavingsParser — it shares parse() unchanged),
purpose-built as a companion to tests/dump_report.py for the
Extract/Classify/Report pipeline refactor (see REFACTORING_ROADMAP.md's
"Architecture Proposal").

The four real fixtures (confirmed via tests/dump_report.py) exercise
deposits, withdrawals, checks, and bank fees well, but don't confirm from
the visible report output alone: the continuation-line description-
extension path, the client-name-in-line skip filter, the two-column check
layout (vs. one real check per line), or that amounts are preserved with
whatever sign the statement itself used (this parser family never forces
a sign convention — unlike Chase/Citi/Wells Fargo's "always positive"
approach, confirmed for BankOfAmericaCreditCardParser's charges bucket
too).

The synthetic text below was constructed and verified against the real
(pre-migration) parser directly, since the section-marker/continuation-
line/regex-findall structure needs exact structural matching.

No real client data — a fictional client name is used specifically to
exercise the client-name-in-line skip filter.
"""
import unittest
from decimal import Decimal

from parsers.bofa import BankOfAmericaCheckingParser

_TEXT = (
    "for May 1, 2026 to May 31, 2026\n"
    "Beginning balance on 5/1/26 $1,000.00\n"
    "Ending balance on 5/31/26 $945.00\n"
    "Deposits and other credits\n"
    "05/05/26 CONTOSO CUSTOMER PAYMENT 500.00\n"
    # Mixed-sign deposit -- must survive with its sign intact.
    "05/06/26 CONTOSO REVERSAL ADJUSTMENT -25.00\n"
    "Withdrawals and other debits\n"
    # Continuation-line description extension, plus the client-name-in-line
    # skip filter (the second continuation line must be excluded).
    "05/10/26 CONTOSO OFFICE SUPPLIES DES:PURCHASE ID:12345 -80.00\n"
    "  additional description continued here\n"
    "  Bravo Studio LLC\n"
    # Mixed-sign withdrawal -- must survive with its sign intact.
    "05/11/26 CONTOSO REFUND CREDIT 40.00\n"
    "Total withdrawals and other debits\n"
    "Checks\n"
    # One-column check. Check numbers are 6 digits, not 4, since a quoted
    # 4-digit string unconditionally trips the PII scanner's account-number
    # pattern with no allowlist path (same issue hit in the Wells Fargo
    # Checking synthetic test) -- the parser's own check regex accepts any
    # digit length, so this is a no-cost substitution.
    "05/15/26 100156 -150.00\n"
    # Two-column check (two checks on the same physical line).
    "05/16/26 100378 -95.00 05/17/26 100449* -220.00\n"
    "Total checks\n"
    "Total service fees -$25.00\n"
    "Daily ledger balances\n"
)


def _d(x):
    return Decimal(str(x))


class BofaCheckingSyntheticPipelineTest(unittest.TestCase):
    def _parser(self):
        p = BankOfAmericaCheckingParser.__new__(BankOfAmericaCheckingParser)
        p.client_name = "Bravo Studio LLC"
        p.beginning_balance = None
        p.ending_balance = None
        p.credits = []
        p.debits = []
        p.checks = []
        p.service_fees = Decimal('0')
        p.closing_date = None
        p.credit_card_payments = []
        p.text = _TEXT
        return p

    def test_balances_and_closing_date_extracted(self):
        p = self._parser()
        p.parse()
        self.assertEqual(p.beginning_balance, _d('1000.00'))
        self.assertEqual(p.ending_balance, _d('945.00'))
        self.assertEqual(p.closing_date, '05/31/26')

    def test_plain_deposit_and_withdrawal(self):
        p = self._parser()
        p.parse()
        credit_vendors = {c['vendor']: c for c in p.credits}
        debit_vendors = {d['vendor'] for d in p.debits}
        self.assertIn('CONTOSO CUSTOMER PAYMENT', credit_vendors)
        self.assertEqual(credit_vendors['CONTOSO CUSTOMER PAYMENT']['amount'], _d('500.00'))
        self.assertTrue(any('CONTOSO OFFICE SUPPLIES' in v for v in debit_vendors))

    def test_negative_deposit_sign_preserved(self):
        p = self._parser()
        p.parse()
        credit_vendors = {c['vendor']: c for c in p.credits}
        self.assertIn('CONTOSO REVERSAL ADJUSTMENT', credit_vendors)
        self.assertEqual(credit_vendors['CONTOSO REVERSAL ADJUSTMENT']['amount'], _d('-25.00'))

    def test_positive_withdrawal_sign_preserved(self):
        p = self._parser()
        p.parse()
        debit_vendors = {d['vendor']: d for d in p.debits}
        self.assertIn('CONTOSO REFUND CREDIT', debit_vendors)
        self.assertEqual(debit_vendors['CONTOSO REFUND CREDIT']['amount'], _d('40.00'))

    def test_continuation_line_extends_description(self):
        p = self._parser()
        p.parse()
        vendors = {d['vendor'] for d in p.debits}
        matching = [v for v in vendors if v.startswith('CONTOSO OFFICE SUPPLIES')]
        self.assertEqual(len(matching), 1)
        self.assertIn('additional description continued here', matching[0])

    def test_client_name_in_continuation_line_excluded(self):
        p = self._parser()
        p.parse()
        vendors = {d['vendor'] for d in p.debits}
        matching = [v for v in vendors if v.startswith('CONTOSO OFFICE SUPPLIES')]
        self.assertNotIn('Bravo Studio LLC', matching[0])

    def test_one_column_check(self):
        p = self._parser()
        p.parse()
        by_num = {c['check_number']: c for c in p.checks}
        self.assertIn('100156', by_num)
        self.assertEqual(by_num['100156']['amount'], _d('-150.00'))

    def test_two_column_check_line_captures_both(self):
        p = self._parser()
        p.parse()
        by_num = {c['check_number']: c for c in p.checks}
        self.assertIn('100378', by_num)
        self.assertIn('100449', by_num)
        self.assertEqual(by_num['100378']['amount'], _d('-95.00'))
        self.assertEqual(by_num['100449']['amount'], _d('-220.00'))

    def test_total_service_fees_extracted(self):
        p = self._parser()
        p.parse()
        self.assertEqual(p.service_fees, _d('25.00'))

    def test_bucket_counts_exhaustive(self):
        p = self._parser()
        p.parse()
        self.assertEqual(len(p.credits), 2)
        self.assertEqual(len(p.debits), 2)
        self.assertEqual(len(p.checks), 3)

    def test_report_balances(self):
        p = self._parser()
        p.parse()
        report = p.generate_report()
        self.assertIn('Balance verification: PASSED', report)

    def test_balances_and_service_fees_with_multi_space_labels(self):
        # pdftotext -layout column alignment can insert many spaces between
        # words in a label; contains_label() must still match. Regression
        # test for REFACTORING_ROADMAP.md's "Literal single-space label
        # gates across every bank parser".
        text = _TEXT.replace(
            "Beginning balance on 5/1/26 $1,000.00",
            "Beginning    balance    on 5/1/26 $1,000.00",
        ).replace(
            "Ending balance on 5/31/26 $945.00",
            "Ending    balance    on 5/31/26 $945.00",
        ).replace(
            "Total service fees -$25.00",
            "Total    service    fees -$25.00",
        )
        p = BankOfAmericaCheckingParser.__new__(BankOfAmericaCheckingParser)
        p.client_name = "Bravo Studio LLC"
        p.beginning_balance = None
        p.ending_balance = None
        p.credits = []
        p.debits = []
        p.checks = []
        p.service_fees = Decimal('0')
        p.closing_date = None
        p.credit_card_payments = []
        p.text = text
        p.parse()
        self.assertEqual(p.beginning_balance, _d('1000.00'))
        self.assertEqual(p.ending_balance, _d('945.00'))
        self.assertEqual(p.service_fees, _d('25.00'))


_CC_TEXT = (
    "for August 1, 2026 to August 31, 2026\n"
    "Beginning balance on 8/1/26 $13,000.00\n"
    "Ending balance on 8/31/26 $0.00\n"
    "Deposits and other credits\n"
    "08/05/26 CONTOSO CUSTOMER PAYMENT 500.00\n"
    "Withdrawals and other debits\n"
    # Real bug shape (found reconciling Paintbox Hair Studio's August 2026
    # BofA checking statement): this client also holds a BofA savings
    # account, and "Online banking transfer" is the same generic
    # description BofA uses whether the money is paying off a credit card
    # or moving to the client's own savings sub-account. Before the fix,
    # every "Online Banking Transfer" debit was unconditionally merged into
    # the CREDIT CARD PAYMENTS section -- this $9,500.00 transfer explicitly
    # names its destination as a savings account (SAV 0295) and must be
    # reported as a plain withdrawal instead, or it misleads QB entry and
    # inflates the Mode E CC-payment tie-out by an amount that was never
    # actually a card payment.
    "08/11/26 ONLINE BANKING TRANSFER TO SAV 0295 -9,500.00\n"
    # A genuine CC bill payment via online banking (no named destination
    # account) must still land in CREDIT CARD PAYMENTS -- existing behavior
    # for the common case must not regress.
    "08/12/26 ONLINE BANKING TRANSFER CONTOSO CARD PMT -4,000.00\n"
    "Total withdrawals and other debits\n"
    "Total service fees -$0.00\n"
    "Daily ledger balances\n"
)


class BofaCheckingOnlineTransferClassificationTest(unittest.TestCase):
    def _parser(self, text):
        p = BankOfAmericaCheckingParser.__new__(BankOfAmericaCheckingParser)
        p.client_name = "Bravo Studio LLC"
        p.beginning_balance = None
        p.ending_balance = None
        p.credits = []
        p.debits = []
        p.checks = []
        p.service_fees = Decimal('0')
        p.closing_date = None
        p.credit_card_payments = []
        p.text = text
        return p

    def test_transfer_to_savings_excluded_from_cc_payments(self):
        p = self._parser(_CC_TEXT)
        p.parse()
        report = p.generate_report()
        cc_section = report.split('CREDIT CARD PAYMENTS')[1].split('=' * 10)[0]
        self.assertNotIn('SAV', cc_section)
        self.assertIn('9,500.00', report)

    def test_transfer_to_savings_lands_in_withdrawals(self):
        p = self._parser(_CC_TEXT)
        p.parse()
        report = p.generate_report()
        withdrawals_section = report.split('WITHDRAWALS AND DEBITS')[1].split('=' * 10)[0]
        self.assertIn('Sav', withdrawals_section)

    def test_plain_online_transfer_still_treated_as_cc_payment(self):
        # Regression guard: a transfer with no named SAV/CHK destination
        # keeps the pre-fix behavior of being merged into CREDIT CARD
        # PAYMENTS -- only explicitly-named-destination transfers change.
        p = self._parser(_CC_TEXT)
        p.parse()
        report = p.generate_report()
        cc_section = report.split('CREDIT CARD PAYMENTS')[1].split('=' * 10)[0]
        self.assertIn('4,000.00', cc_section)

    def test_balance_still_ties_with_split_transfer(self):
        p = self._parser(_CC_TEXT)
        p.parse()
        report = p.generate_report()
        self.assertIn('Balance verification: PASSED', report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
