"""
test_mark_payroll_done.py
--------------------------
Unit tests for mark_payroll_done.py — the retroactive "log this payroll run
as done" tool for when QuickBooks entry happened outside a normal session.

These tests use only synthetic client configs and temp log files — no real
client data, no Drive, no git push (monkeypatched out). They verify the
write path against payroll_log.csv, reconciliation_log.csv, and
recon_log.json without touching the private clients repo.

Run:
    python3 -m pytest tests/test_mark_payroll_done.py -v
"""

import csv
import json
import sys
import tempfile
import shutil
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import mark_payroll_done                       # noqa: E402
from parsers.base import ClientRegistry        # noqa: E402
import parsers.base as _parsers_base           # noqa: E402


def _write_client(d: Path, filename: str, cfg: dict):
    with open(d / filename, "w") as f:
        json.dump(cfg, f)


class MarkPayrollDoneTest(unittest.TestCase):
    def setUp(self):
        self.clients_dir = Path(tempfile.mkdtemp())
        self.logs_dir = Path(tempfile.mkdtemp())

        _write_client(self.clients_dir, "payroll_key_client.json", {
            "client_name":    "Test Payroll Client",
            "canonical_name": "TEST PAYROLL CLIENT",
            "aliases":        ["test_co"],
            "payroll_key":    "test_co",
            "statement_types": [],
        })
        _write_client(self.clients_dir, "name_only_client.json", {
            "client_name":    "Example Client Two",
            "canonical_name": "EXAMPLE CLIENT TWO",
            "aliases":        [],
            "statement_types": [],
        })

        self._orig_registry = _parsers_base._registry
        _parsers_base._registry = ClientRegistry(clients_dir=str(self.clients_dir))

        self.payroll_log = self.logs_dir / "payroll_log.csv"
        self.recon_log = self.logs_dir / "reconciliation_log.csv"
        self.recon_json = self.logs_dir / "recon_log.json"

        self._orig_payroll_path = mark_payroll_done.PAYROLL_LOG_PATH
        self._orig_recon_path = mark_payroll_done.RECON_LOG_PATH
        mark_payroll_done.PAYROLL_LOG_PATH = self.payroll_log
        mark_payroll_done.RECON_LOG_PATH = self.recon_log

        self._orig_git_push = mark_payroll_done._git_push_logs
        mark_payroll_done._git_push_logs = lambda label: None

        import os
        self._orig_logs_dir_env = os.environ.get("BOOKKEEPING_LOGS_DIR")
        self._orig_no_prompt_env = os.environ.get("BOOKKEEPING_NO_PROMPT")
        os.environ["BOOKKEEPING_LOGS_DIR"] = str(self.logs_dir)
        os.environ["BOOKKEEPING_NO_PROMPT"] = "1"

        self._orig_argv = sys.argv

    def tearDown(self):
        import os
        _parsers_base._registry = self._orig_registry
        mark_payroll_done.PAYROLL_LOG_PATH = self._orig_payroll_path
        mark_payroll_done.RECON_LOG_PATH = self._orig_recon_path
        mark_payroll_done._git_push_logs = self._orig_git_push
        if self._orig_logs_dir_env is None:
            os.environ.pop("BOOKKEEPING_LOGS_DIR", None)
        else:
            os.environ["BOOKKEEPING_LOGS_DIR"] = self._orig_logs_dir_env
        if self._orig_no_prompt_env is None:
            os.environ.pop("BOOKKEEPING_NO_PROMPT", None)
        else:
            os.environ["BOOKKEEPING_NO_PROMPT"] = self._orig_no_prompt_env
        sys.argv = self._orig_argv
        shutil.rmtree(self.clients_dir)
        shutil.rmtree(self.logs_dir)

    def _run(self, *args):
        sys.argv = ["mark_payroll_done.py", *args]
        mark_payroll_done.main()

    def _read_csv(self, path):
        if not path.exists():
            return []
        with open(path, newline="") as f:
            return list(csv.DictReader(f))

    def _read_recon_json(self):
        if not self.recon_json.exists():
            return []
        with open(self.recon_json) as f:
            return json.load(f)

    def test_writes_all_three_logs_via_payroll_key(self):
        self._run("test_co", "06/05/26", "18234.12")

        payroll_rows = self._read_csv(self.payroll_log)
        self.assertEqual(len(payroll_rows), 1)
        self.assertEqual(payroll_rows[0]["client"], "test_co")
        self.assertEqual(payroll_rows[0]["check_date"], "06/05/26")
        self.assertEqual(payroll_rows[0]["bank_credit"], "18234.12")

        recon_rows = self._read_csv(self.recon_log)
        self.assertEqual(len(recon_rows), 1)
        self.assertEqual(recon_rows[0]["account_type"], "payroll")
        self.assertEqual(recon_rows[0]["statement_date"], "06/05/26")
        self.assertEqual(recon_rows[0]["source"], "mark_payroll_done")

        json_entries = self._read_recon_json()
        payroll_entries = [e for e in json_entries if e.get("account_type") == "payroll"]
        self.assertEqual(len(payroll_entries), 1)
        self.assertEqual(payroll_entries[0]["status"], "DONE")
        print("PASS  test_writes_all_three_logs_via_payroll_key")

    def test_resolves_client_by_name_when_no_payroll_key(self):
        self._run("Example Client Two", "06/05/26", "9450.00")

        payroll_rows = self._read_csv(self.payroll_log)
        self.assertEqual(len(payroll_rows), 1)
        self.assertEqual(payroll_rows[0]["client"], "Example Client Two")
        self.assertEqual(payroll_rows[0]["client_name"], "Example Client Two")
        print("PASS  test_resolves_client_by_name_when_no_payroll_key")

    def test_unknown_client_exits_without_writing(self):
        with self.assertRaises(SystemExit):
            self._run("not_a_real_client", "06/05/26", "100.00")
        self.assertFalse(self.payroll_log.exists())
        self.assertFalse(self.recon_log.exists())
        print("PASS  test_unknown_client_exits_without_writing")

    def test_payroll_log_upserts_same_check_date(self):
        """Re-running for the same client + check_date must update the row in
        place (e.g. a corrected bank_credit), not append a duplicate."""
        self._run("test_co", "06/05/26", "7183.51")
        self._run("test_co", "06/05/26", "7183.99")

        payroll_rows = self._read_csv(self.payroll_log)
        self.assertEqual(len(payroll_rows), 1, "expected upsert, not a second row")
        self.assertEqual(payroll_rows[0]["bank_credit"], "7183.99")
        print("PASS  test_payroll_log_upserts_same_check_date")

    def test_payroll_log_keeps_one_row_per_check_date(self):
        """payroll_log.csv keys on [client, check_date] — one row per run
        date, unlike reconciliation_log.csv which tracks only the latest."""
        self._run("test_co", "04/15/26", "7183.51")
        self._run("test_co", "05/29/26", "8375.30")

        payroll_rows = self._read_csv(self.payroll_log)
        self.assertEqual(len(payroll_rows), 2,
                          "expected one payroll_log row per check_date")

        recon_rows = self._read_csv(self.recon_log)
        self.assertEqual(len(recon_rows), 1,
                          "reconciliation_log.csv should track only the latest run")
        self.assertEqual(recon_rows[0]["statement_date"], "05/29/26")
        print("PASS  test_payroll_log_keeps_one_row_per_check_date")

    def test_invalid_amount_exits_without_writing(self):
        with self.assertRaises(SystemExit):
            self._run("test_co", "06/05/26", "not-a-number")
        self.assertFalse(self.payroll_log.exists())
        print("PASS  test_invalid_amount_exits_without_writing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
