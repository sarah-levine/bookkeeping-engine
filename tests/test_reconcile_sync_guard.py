"""
test_reconcile_sync_guard.py
------------------------------
Regression coverage for _sync_logs_before_write() (reconcile_comprehensive.py),
the pull-before-write safeguard added to close a real incident: the private
Bookkeeping-clients repo had a commit titled "Restore 14 Needles Studio
Square payroll log entries wiped by a concurrent session" -- a stale local
clone silently clobbering another session's log writes, because
reconcile_comprehensive.py only ever pushed (tools.github_clients.sync_up())
and never pulled first.

tools.github_clients.sync_down() already existed to solve exactly this (pull
recon_log.json / reconciliation_log.csv / payroll_log.csv fresh via the
GitHub REST API, no git required) but was never called from the live write
path. This test covers the function that now wires it in, mocking
sync_down() entirely -- no real network or GITHUB_PAT_BOOKKEEPING needed,
and no real fixture exercises this since it's infrastructure, not a parser.
"""
import unittest
from unittest import mock

from reconcile_comprehensive import _sync_logs_before_write


class SyncLogsBeforeWriteTest(unittest.TestCase):
    def test_dry_run_skips_sync_entirely(self):
        with mock.patch("tools.github_clients.sync_down") as _sync:
            result = _sync_logs_before_write(dry_run=True, no_prompt=False)
        _sync.assert_not_called()
        self.assertTrue(result)

    def test_dry_run_skips_sync_even_with_no_prompt(self):
        with mock.patch("tools.github_clients.sync_down") as _sync:
            result = _sync_logs_before_write(dry_run=True, no_prompt=True)
        _sync.assert_not_called()
        self.assertTrue(result)

    def test_successful_sync_pulls_logs_only_and_proceeds(self):
        with mock.patch("tools.github_clients.sync_down") as _sync:
            result = _sync_logs_before_write(dry_run=False, no_prompt=False)
        _sync.assert_called_once_with(include_configs=False)
        self.assertTrue(result)

    def test_failed_sync_no_prompt_warns_and_proceeds_anyway(self):
        with mock.patch("tools.github_clients.sync_down", side_effect=RuntimeError("boom")):
            result = _sync_logs_before_write(dry_run=False, no_prompt=True)
        # --no-prompt can't block on input by definition -- best-effort
        # continue, but the warning banner (not asserted here, just the
        # non-blocking behavior) is what makes this "loud" rather than silent.
        self.assertTrue(result)

    def test_failed_sync_interactive_yes_proceeds(self):
        with mock.patch("tools.github_clients.sync_down", side_effect=RuntimeError("boom")), \
             mock.patch("builtins.input", return_value="y"):
            result = _sync_logs_before_write(dry_run=False, no_prompt=False)
        self.assertTrue(result)

    def test_failed_sync_interactive_no_skips_writes(self):
        with mock.patch("tools.github_clients.sync_down", side_effect=RuntimeError("boom")), \
             mock.patch("builtins.input", return_value="n"):
            result = _sync_logs_before_write(dry_run=False, no_prompt=False)
        self.assertFalse(result)

    def test_failed_sync_interactive_blank_defaults_to_no(self):
        with mock.patch("tools.github_clients.sync_down", side_effect=RuntimeError("boom")), \
             mock.patch("builtins.input", return_value=""):
            result = _sync_logs_before_write(dry_run=False, no_prompt=False)
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
