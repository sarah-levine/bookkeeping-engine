"""
test_adp_payroll_tipped_pay_by_pay.py
--------------------------------------
Regression coverage for a real bug: run_adp_payroll_tipped() accepted a
Payroll Liability PDF as a second positional argument but never read it --
the "Pay-by-Pay Insurance" amount (which varies every pay period) silently
fell back to a static per-client config constant instead. Found reconciling
Acme Salon LLC's real August 2026 payroll runs: the journal entry's
Pay-by-Pay figures never matched the actual ADP debit shown on the bank
statement, because they came from a stale config default (22.55) instead
of that period's real Liability PDF amount (29.95 / 25.21 on two
consecutive runs).

Run:
    python3 tests/test_adp_payroll_tipped_pay_by_pay.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from payroll_clients.adp_payroll_tipped import (  # noqa: E402
    parse_pay_by_pay, resolve_pay_by_pay,
)

# Real Liability PDF text shape (fictional client name/numbers) --
# extract_text() (pdfplumber) squishes adjacent words together with no
# space on this PDF's layout (confirmed against the real fixture), so the
# regex -- and this synthetic text -- must match that squished form, not a
# nicely spaced hand-typed approximation. The amount appears twice (Other
# Transfers section, then the Total-For section) with the same value, same
# as the real fixture.
_LIABILITY_TEXT = """\
PayrollLiability
TotalCashRequired $6,710.76
OtherTransfers FullServiceDirectDeposit(FSDD) 4,573.86 2Employee
Transactions
Pay-by-PayInsurance 25.21
TotalDirectDeposit(FSDD) $4,573.86
TotalPay-by-PayInsurance $25.21
TotalTaxes $2,111.69
TotalAmountADPDebitedfromyour $6,710.76
Account(s)
TotalFor8/31/2026-Payroll1
TotalDirectDeposit(FSDD) $4,573.86
TotalPay-by-PayInsurance $25.21
Company:AcmeSalonLLC
"""

_NO_PAY_BY_PAY_TEXT = "PayrollLiability\nTotalCashRequired $100.00\n"


class ParsePayByPayTest(unittest.TestCase):
    def test_found(self):
        self.assertEqual(parse_pay_by_pay(_LIABILITY_TEXT), 25.21)

    def test_not_found_returns_none(self):
        self.assertIsNone(parse_pay_by_pay(_NO_PAY_BY_PAY_TEXT))


class ResolvePayByPayPrecedenceTest(unittest.TestCase):
    def test_cli_override_wins_over_everything(self):
        cfg = {"workers_comp_refund": 22.55}
        amount, warning = resolve_pay_by_pay(99.99, _LIABILITY_TEXT, cfg)
        self.assertEqual(amount, 99.99)
        self.assertIsNone(warning)

    def test_liability_pdf_value_used_when_no_override(self):
        # Real bug shape: config default (22.55) must NOT win when a real
        # per-run Liability PDF value (25.21) is available.
        cfg = {"workers_comp_refund": 22.55}
        amount, warning = resolve_pay_by_pay(None, _LIABILITY_TEXT, cfg)
        self.assertEqual(amount, 25.21)
        self.assertIsNone(warning)

    def test_falls_back_to_config_default_with_warning_when_neither_given(self):
        cfg = {"workers_comp_refund": 22.55}
        amount, warning = resolve_pay_by_pay(None, None, cfg)
        self.assertEqual(amount, 22.55)
        self.assertIsNotNone(warning)
        self.assertIn("will NOT match", warning)

    def test_falls_back_to_config_default_when_liability_text_has_no_match(self):
        cfg = {"workers_comp_refund": 22.55}
        amount, warning = resolve_pay_by_pay(None, _NO_PAY_BY_PAY_TEXT, cfg)
        self.assertEqual(amount, 22.55)
        self.assertIsNotNone(warning)

    def test_falls_back_to_hardcoded_default_when_config_has_no_key(self):
        amount, warning = resolve_pay_by_pay(None, None, {})
        self.assertEqual(amount, 22.55)
        self.assertIsNotNone(warning)


if __name__ == "__main__":
    unittest.main(verbosity=2)
