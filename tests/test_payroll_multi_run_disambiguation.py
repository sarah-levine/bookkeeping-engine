"""
test_payroll_multi_run_disambiguation.py
------------------------------------------
Regression coverage for a real bug: ADP occasionally splits one check date
into multiple separate payroll runs (e.g. a regular payroll plus a same-day
1099/contractor-only run, each its own PDF with its own Run Number, but the
same check date). append_payroll_log/append_digest_log key their upserts on
(client, check_date) -- with no way to tell the runs apart, logging the
second run silently overwrote the first run's entry in payroll_log.csv and
recon_log.json, losing its BALANCED confirmation record entirely.

Caught live reconciling a real client's two same-date FCBA-style runs
(Run Number 0167 "Payroll 1" and Run Number 0168 "Payroll 2", both dated
8/19/2026) before the second one was ever logged -- ADP's own PDF footer
text ("Checkdate:8/19/2026-Payroll2 RunNumber:0168...") is the only signal
that distinguishes them.

Uses fictional/synthetic header text -- no real client data.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from payroll_clients.base import parse_header
from tests._registry_test_utils import install_example_registry, restore_registry
from log_utils import _assert_known_client


class ParseHeaderPayrollRunTest(unittest.TestCase):
    def test_single_run_header_has_no_payroll_run_key(self):
        # Plain single-payroll-per-date header (the historical, common case)
        # must not gain a payroll_run key -- backward compatible with every
        # existing log row, none of which carry a run suffix.
        text = "Checkdate:8/19/2026 RunNumber:0167 25350949-KE/9ZD\nPayPeriod:08/01/2026to:08/15/2026"
        header = parse_header(text)
        self.assertEqual(header["check_date"], "8/19/2026")
        self.assertNotIn("payroll_run", header)

    def test_first_of_multiple_runs_captured(self):
        text = "Checkdate:8/19/2026-Payroll1 RunNumber:0167 25350949-KE/9ZD\nPayPeriod:08/01/2026to:08/15/2026"
        header = parse_header(text)
        self.assertEqual(header["check_date"], "8/19/2026")
        self.assertEqual(header["payroll_run"], "1")

    def test_second_of_multiple_runs_captured(self):
        text = "Checkdate:8/19/2026-Payroll2 RunNumber:0168 25350949-KE/9ZD\nPayPeriod:08/01/2026to:08/15/2026"
        header = parse_header(text)
        self.assertEqual(header["check_date"], "8/19/2026")
        self.assertEqual(header["payroll_run"], "2")


class AssertKnownClientMultiRunSuffixTest(unittest.TestCase):
    """Uses the repo's own public clients/example_client.json ("Acme Inc",
    payroll_key "acme") -- no real client data needed."""

    def setUp(self):
        self._previous_registry = install_example_registry()

    def tearDown(self):
        restore_registry(self._previous_registry)

    def test_payroll_key_run_suffix_resolves(self):
        _assert_known_client("acme_run2")  # must not raise / prompt

    def test_display_name_payroll_n_suffix_resolves(self):
        _assert_known_client("Acme Inc — Payroll 2")

    def test_higher_run_numbers_also_resolve(self):
        _assert_known_client("acme_run3")
        _assert_known_client("Acme Inc — Payroll 3")


if __name__ == "__main__":
    unittest.main(verbosity=2)
