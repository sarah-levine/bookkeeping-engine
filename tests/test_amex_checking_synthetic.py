"""
test_amex_checking_synthetic.py
---------------------------------
Synthetic regression coverage for AmexCheckingParser, purpose-built as a
companion to tests/dump_report.py for the Extract/Classify/Report pipeline
refactor (see REFACTORING_ROADMAP.md's "Architecture Proposal").

The one real fixture (confirmed via tests/dump_report.py) exercises
credits, debits, checks, interest earned, and ADP payroll well — but
doesn't confirm from visible report output alone: the parse()-called-twice
idempotency property that generate_report() actually relies on (it calls
self.parse() itself, unlike every other parser in this rollout), the
continuation-line skip filter for 'ID '-prefixed/reference-code lines, or
that the check-labeled-debit post-loop filter actually removes something.

The synthetic text below was constructed and verified against the real
(pre-migration) parser directly, since the multi-line transaction/
continuation structure needs exact structural matching.

No real client data — client_name is left unset (None).
"""
import unittest
from decimal import Decimal

from parsers.amex import AmexCheckingParser

_TEXT = (
    "Statement Date: 01/31/2026\n"
    "Account Ending: *19440\n"
    "Beginning Balance as of 01/01/2026 $100,000.00\n"
    "Ending Balance as of 01/31/2026 $98,807.50\n"
    "01/05/2026 Online Transfer / Payment: Credit      $425.00            $100,425.00\n"
    "  CONTOSO VENDOR TRANSFER\n"
    "01/06/2026 Online Transfer / Payment: Debit       $80.00             $100,345.00\n"
    "  CONTOSO DEBIT VENDOR\n"
    # Interest deposit -- must accumulate into self.interest_earned, not
    # appear as a row in self.credits.
    "01/07/2026 Interest Deposit                       $12.50             $100,357.50\n"
    # Continuation lines: an 'ID '-prefixed reference line must be skipped,
    # falling through to the next continuation line as the real description.
    "01/08/2026 Some Type: Debit                       $50.00             $100,307.50\n"
    "  ID 000000000000000\n"
    "  CONTOSO REAL VENDOR\n"
    # Check-labeled debit (no continuation line, so its description falls
    # back to the header-derived label "Check: Withdrawal") -- must be
    # removed from self.debits by the post-loop filter, since it's already
    # accounted for in the Checks Paid Summary section below.
    "01/09/2026 Check: Withdrawal                      $200.00            $100,107.50\n"
    "Checks Paid Summary\n"
    "312   01/12/2026   $1,500.00\n"
)


def _d(x):
    return Decimal(str(x))


class AmexCheckingSyntheticPipelineTest(unittest.TestCase):
    def _parser(self):
        p = AmexCheckingParser.__new__(AmexCheckingParser)
        p.client_name = None
        p.beginning_balance = None
        p.ending_balance = None
        p.statement_date = ''
        p.account_number = ''
        p.credits = []
        p.debits = []
        p.checks = []
        p.interest_earned = Decimal('0')
        p.text = _TEXT
        return p

    def test_metadata_extracted(self):
        p = self._parser()
        p.parse()
        self.assertEqual(p.beginning_balance, _d('100000.00'))
        self.assertEqual(p.ending_balance, _d('98807.50'))
        self.assertEqual(p.statement_date, '01/31/2026')
        self.assertEqual(p.account_number, '19440')

    def test_plain_credit_and_debit(self):
        p = self._parser()
        p.parse()
        credit_vendors = {c['description']: c for c in p.credits}
        debit_vendors = {d['description']: d for d in p.debits}
        self.assertIn('CONTOSO VENDOR TRANSFER', credit_vendors)
        self.assertEqual(credit_vendors['CONTOSO VENDOR TRANSFER']['amount'], _d('425.00'))
        self.assertIn('CONTOSO DEBIT VENDOR', debit_vendors)
        self.assertEqual(debit_vendors['CONTOSO DEBIT VENDOR']['amount'], _d('80.00'))

    def test_interest_deposit_accumulates_as_scalar_not_row(self):
        p = self._parser()
        p.parse()
        self.assertEqual(p.interest_earned, _d('12.50'))
        self.assertNotIn('Interest Deposit', {c['description'] for c in p.credits})

    def test_id_prefixed_continuation_line_skipped(self):
        p = self._parser()
        p.parse()
        debit_vendors = {d['description'] for d in p.debits}
        self.assertIn('CONTOSO REAL VENDOR', debit_vendors)
        self.assertNotIn('ID 000000000000000', debit_vendors)

    def test_check_labeled_debit_removed_by_post_loop_filter(self):
        p = self._parser()
        p.parse()
        self.assertFalse(any('CHECK' in d['description'].upper() for d in p.debits))

    def test_checks_paid_summary_captured(self):
        p = self._parser()
        p.parse()
        self.assertEqual(len(p.checks), 1)
        self.assertEqual(p.checks[0]['check_number'], '312')
        self.assertEqual(p.checks[0]['amount'], _d('1500.00'))

    def test_bucket_counts_exhaustive(self):
        p = self._parser()
        p.parse()
        self.assertEqual(len(p.credits), 1)
        self.assertEqual(len(p.debits), 2)
        self.assertEqual(len(p.checks), 1)

    def test_parse_is_idempotent_when_called_twice(self):
        # generate_report() calls self.parse() itself -- unlike every other
        # parser in this rollout -- so parse() must produce identical
        # results on a second call, not double every transaction.
        p = self._parser()
        p.parse()
        first_credits = list(p.credits)
        first_debits = list(p.debits)
        first_checks = list(p.checks)
        first_interest = p.interest_earned
        p.parse()
        self.assertEqual(p.credits, first_credits)
        self.assertEqual(p.debits, first_debits)
        self.assertEqual(p.checks, first_checks)
        self.assertEqual(p.interest_earned, first_interest)

    def test_report_balances(self):
        p = self._parser()
        report = p.generate_report()
        self.assertIn('Balance verification: PASSED', report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
