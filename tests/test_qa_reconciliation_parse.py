"""
test_qa_reconciliation_parse.py
--------------------------------
Regression coverage for a real bug: qa_reconciliation.py's parse_recon_output()
never matched any row in a checking statement's WITHDRAWALS AND DEBITS,
PAYROLL, or CREDIT CARD PAYMENTS sections. Those sections print amounts with
a leading '-' right after the '$' (e.g. "$        -311.49"), which row_re/
checks_re's character class ([\\d,]+) didn't allow for -- every row in those
three sections silently failed to match, leaving data['payments'] empty.

Found live reconciling JoJo Hair Studio Inc's 09/30/2026 BofA checking
statement for a QB QA comparison (Mode D): the "CHECKS AND PAYMENTS" table
showed every report-side amount as "--" / $0.00 even though the real report
had 21 payment rows totaling $31,068.40 -- exactly what QuickBooks itself
showed as cleared.

Run:
    python3 tests/test_qa_reconciliation_parse.py
"""

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from qa_reconciliation import parse_recon_output  # noqa: E402

# Real report text shape (fictional client/vendor names), trimmed to the
# sections this bug affects.
_CHECKING_REPORT = """\
================================================================================
BANK OF AMERICA BUSINESS CHECKING
ACME SALON LLC
Statement Period: September 1, 2026 - September 30, 2026
================================================================================

STATEMENT SUMMARY
--------------------------------------------------------------------------------
Beginning Balance:                       $   25,521.23
--------------------------------------------------------------------------------
Ending Balance:                          $   29,955.23
--------------------------------------------------------------------------------

================================================================================
CREDITS / DEPOSITS

Date         Description                                                  Amount
--------------------------------------------------------------------------------
09/30/26     Deposit in Transit                                $      35,508.40
                                                                -----------------
TOTAL CREDITS:                                                  $      35,508.40

================================================================================
WITHDRAWALS AND DEBITS

Date         Description                                                  Amount
--------------------------------------------------------------------------------
09/02/26     BRAVO INSURANCE                                    $        -115.00
09/23/26     Life Insurance Co                                  $        -345.16
                                                                -----------------
TOTAL WITHDRAWALS AND DEBITS:                                   $        -460.16

================================================================================
CHECKS

Check #    Date         Payee                                             Amount
--------------------------------------------------------------------------------
1095       09/10/26     Contoso Signs                                $    -3,600.00
                                                                   ---------------
TOTAL CHECKS:                                                      $    -3,600.00

================================================================================
PAYROLL

Date         Description                                                  Amount
--------------------------------------------------------------------------------
09/14/26     ADP WAGE PAY                                       $      -8,238.42
                                                                -----------------
TOTAL PAYROLL:                                                  $      -8,238.42

================================================================================
CREDIT CARD PAYMENTS

Date         Description                                                  Amount
--------------------------------------------------------------------------------
09/08/26     CITI CARD ONLINE                                   $      -2,230.03
                                                                -----------------
TOTAL CREDIT CARD PAYMENTS:                                     $      -2,230.03
"""


class ParseReconOutputNegativeAmountsTest(unittest.TestCase):
    def test_withdrawals_section_parsed(self):
        data = parse_recon_output(_CHECKING_REPORT)
        vendors = [p['vendor'] for p in data['payments']]
        self.assertIn('BRAVO INSURANCE', vendors)
        self.assertIn('Life Insurance Co', vendors)

    def test_payroll_and_cc_payments_sections_parsed(self):
        data = parse_recon_output(_CHECKING_REPORT)
        amounts = {p['vendor']: p['amount'] for p in data['payments']}
        self.assertEqual(amounts['ADP WAGE PAY'], Decimal('8238.42'))
        self.assertEqual(amounts['CITI CARD ONLINE'], Decimal('2230.03'))

    def test_amounts_stored_as_positive_magnitude(self):
        # QB's reconcile grid always shows positive amounts regardless of
        # debit/credit -- the report's negative sign is a display
        # convention, not a value to carry through to the comparison.
        data = parse_recon_output(_CHECKING_REPORT)
        for p in data['payments']:
            self.assertGreater(p['amount'], Decimal('0'))

    def test_checks_section_parsed_with_negative_amount(self):
        data = parse_recon_output(_CHECKING_REPORT)
        checks = [p for p in data['payments'] if 'Contoso' in p['vendor']]
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]['amount'], Decimal('3600.00'))

    def test_payments_total_matches_sum_of_all_three_sections(self):
        data = parse_recon_output(_CHECKING_REPORT)
        total = sum(p['amount'] for p in data['payments'])
        # 115.00 + 345.16 + 3600.00 + 8238.42 + 2230.03
        self.assertEqual(total, Decimal('14528.61'))

    def test_credits_section_unaffected_by_fix(self):
        # Credits section never had negative amounts -- confirms the fix
        # didn't change its (already-working) behavior.
        data = parse_recon_output(_CHECKING_REPORT)
        self.assertEqual(len(data['credits']), 1)
        self.assertEqual(data['credits'][0]['amount'], Decimal('35508.40'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
