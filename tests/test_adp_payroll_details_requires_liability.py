"""
test_adp_payroll_details_requires_liability.py
------------------------------------------------
Regression test for run_adp_payroll_details() in payroll_clients/adp_payroll_details.py.

Previously, omitting the Payroll Liability PDF was accepted silently: Workers
Comp / Pay-by-Pay defaulted to $0.00 with only a printed warning, and a fully
formatted, BALANCED-looking journal entry was still generated and could be
entered into QuickBooks. adp_payroll_details has no config-level default for
this figure (unlike adp_payroll_tipped's workers_comp_refund) -- the
Liability PDF is the only source for it, so a missing Liability PDF means a
real, usually-nonzero dollar amount was silently replaced with a wrong one.
Caught live: a real payroll run's report looked complete and balanced with
WC=$0.00 only because the Liability PDF hadn't been provided yet, not
because WC was actually zero that period.

The function must now error out (sys.exit) before parsing or printing
anything when no liability PDF is given, and name which file is missing.

No real PDFs needed -- this only needs to prove the function bails out
before ever reaching parse_payroll_details(), so that function is patched
to raise if called (which would fail the test if the guard were removed).
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from payroll_clients import adp_payroll_details as mod


class RequiresLiabilityPdfTest(unittest.TestCase):
    def test_missing_liability_pdf_exits_without_parsing(self):
        with patch.object(mod, "parse_payroll_details",
                           side_effect=AssertionError("should not be called")) as mock_parse, \
             patch.object(mod, "load_config", return_value={"client_name": "Test Client"}):
            with self.assertRaises(SystemExit) as ctx:
                mod.run_adp_payroll_details(["details.pdf"], "test_client.json")

        self.assertEqual(ctx.exception.code, 1)
        mock_parse.assert_not_called()

    def test_no_args_at_all_also_exits(self):
        with self.assertRaises(SystemExit) as ctx:
            mod.run_adp_payroll_details([], "test_client.json")
        self.assertEqual(ctx.exception.code, 1)

    def test_both_pdfs_given_proceeds_past_the_guard(self):
        # Doesn't need real PDFs to reach _build_journal -- just needs to
        # prove the guard itself doesn't block the two-argument case. Patches
        # everything downstream of the guard so this stays a unit test of
        # the guard, not an integration test of the parsers.
        fake_data = {"check_date": "01/01/26"}
        with patch.object(mod, "load_config", return_value={"client_name": "Test Client"}), \
             patch.object(mod, "verify_same_check_date") as mock_verify, \
             patch.object(mod, "parse_payroll_details", return_value=fake_data) as mock_parse, \
             patch.object(mod, "parse_liability", return_value={"wc": 12.34}) as mock_liab, \
             patch.object(mod, "_build_journal", return_value=[]) as mock_build, \
             patch.object(mod, "print_journal_table"), \
             patch.object(mod, "_qb_confirm", return_value=False):
            mod.run_adp_payroll_details(["details.pdf", "liability.pdf"], "test_client.json")

        mock_verify.assert_called_once()
        mock_parse.assert_called_once()
        mock_liab.assert_called_once_with("liability.pdf")
        mock_build.assert_called_once_with(fake_data, 12.34, {"client_name": "Test Client"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
