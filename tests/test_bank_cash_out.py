"""
test_bank_cash_out.py
----------------------
Regression coverage for the payroll_log.csv `bank_credit` bug: it used to
be the journal's full Credit total (check_balance()'s total_c), which
silently includes non-cash reclass lines that never touch the bank -- e.g.
an S-corp 2% shareholder medical premium (debited to wages, credited to a
shareholder-medical account) or a pre-tax health insurance deduction
(credited to a health-insurance-payable account). Those lines balance the
journal but aren't real ADP bank debits, so bank_credit overstated the
true cash-out whenever such a line was present -- silently corrupting
Mode C's payroll<->checking tie-out for every affected run.

Caught live reconciling a real Paintbox Hair Studio Oct 2026 payroll run
(S-corp 2% medical premium $948.13 inflated bank_credit from the true
$6,855.88 cash-out to $7,830.93). adp_payroll_tipped (shareholder medical),
adp_payroll_details (shareholder medical), and adp_payroll_departments
(health insurance pre-tax) all share the same pattern -- see
REFACTORING_ROADMAP.md for the historical payroll_log.csv entries this
may have affected that could not be backfilled without the original
source PDFs.
"""
import csv
import json
import sys
import tempfile
import shutil
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from payroll_clients.base import bank_cash_out, append_payroll_log, make_row
import payroll_clients.base as _base_mod
from parsers.base import ClientRegistry
import parsers.base as _parsers_base


class BankCashOutUnitTest(unittest.TestCase):
    """Direct tests of bank_cash_out(), no file I/O."""

    def test_only_bank_account_rows_counted(self):
        rows = [
            make_row("10/30/26", "Salaries and Wages", debit=4678.57),
            make_row("10/30/26", "Salaries and Wages", debit=948.13, memo="2%"),
            make_row("10/30/26", "LLC BOFA Checking 6681", credit=4669.04, memo="Netpay"),
            make_row("10/30/26", "LLC BOFA Checking 6681", credit=2153.76, memo="Taxes"),
            make_row("10/30/26", "LLC BOFA Checking 6681", credit=33.08, memo="Pay by Pay Refund"),
            make_row("10/30/26", "2% Shareholder medical Premium", credit=948.13, memo="2%"),
        ]
        # Real cash out is only the three bank-account credit rows -- the
        # $948.13 shareholder medical credit must NOT be counted.
        self.assertEqual(bank_cash_out(rows, "LLC BOFA Checking 6681"), 6855.88)

    def test_debit_to_bank_account_reduces_cash_out(self):
        rows = [
            make_row("1/1/26", "BANK", credit=1000.00),
            make_row("1/1/26", "BANK", debit=100.00),
        ]
        self.assertEqual(bank_cash_out(rows, "BANK"), 900.00)

    def test_no_bank_account_rows_returns_zero(self):
        rows = [make_row("1/1/26", "Other Account", credit=500.00)]
        self.assertEqual(bank_cash_out(rows, "BANK"), 0.0)

    def test_falsy_bank_account_returns_zero(self):
        rows = [make_row("1/1/26", "BANK", credit=500.00)]
        self.assertEqual(bank_cash_out(rows, None), 0.0)
        self.assertEqual(bank_cash_out(rows, ""), 0.0)


class AppendPayrollLogBankCreditTest(unittest.TestCase):
    """Integration: append_payroll_log() writes the bank-account-filtered
    amount to payroll_log.csv's bank_credit column when bank_account is
    supplied, and falls back to the old full-credit-total behavior when it
    isn't (so callers that don't pass one, e.g. adp_labor_distribution,
    keep working unchanged)."""

    def setUp(self):
        self.clients_dir = Path(tempfile.mkdtemp())
        self.logs_dir = Path(tempfile.mkdtemp())
        with open(self.clients_dir / "test_client.json", "w") as f:
            json.dump({
                "client_name":    "Test Salon LLC",
                "canonical_name": "TEST SALON LLC",
                "aliases":        [],
                "payroll_key":    "test_salon",
                "statement_types": [],
            }, f)

        self._orig_registry = _parsers_base._registry
        _parsers_base._registry = ClientRegistry(clients_dir=str(self.clients_dir))

        self.payroll_log = self.logs_dir / "payroll_log.csv"
        self._orig_payroll_path = _base_mod.PAYROLL_LOG_PATH
        _base_mod.PAYROLL_LOG_PATH = self.payroll_log

    def tearDown(self):
        _parsers_base._registry = self._orig_registry
        _base_mod.PAYROLL_LOG_PATH = self._orig_payroll_path
        shutil.rmtree(self.clients_dir)
        shutil.rmtree(self.logs_dir)

    def _read_bank_credit(self):
        with open(self.payroll_log) as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        return rows[0]["bank_credit"]

    def test_bank_credit_excludes_non_cash_reclass_line(self):
        rows = [
            make_row("10/30/26", "Wages", debit=4678.57),
            make_row("10/30/26", "Wages", debit=948.13, memo="2%"),
            make_row("10/30/26", "BANK", credit=4669.04, memo="Netpay"),
            make_row("10/30/26", "BANK", credit=2153.76, memo="Taxes"),
            make_row("10/30/26", "BANK", credit=33.08, memo="Pay by Pay Refund"),
            make_row("10/30/26", "Shareholder Medical", credit=948.13, memo="2%"),
        ]
        append_payroll_log("test_salon", "Test Salon LLC", "10/30/2026", rows,
                            bank_account="BANK")
        # True cash-out ($6,855.88), not the full journal credit total ($7,830.93).
        self.assertEqual(self._read_bank_credit(), "6855.88")

    def test_without_bank_account_falls_back_to_full_credit_total(self):
        rows = [
            make_row("1/1/26", "Wages", debit=100.00),
            make_row("1/1/26", "BANK", credit=100.00),
        ]
        append_payroll_log("test_salon", "Test Salon LLC", "01/01/2026", rows)
        self.assertEqual(self._read_bank_credit(), "100.00")


if __name__ == "__main__":
    unittest.main(verbosity=2)
