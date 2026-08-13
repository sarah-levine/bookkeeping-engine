"""
test_assert_known_client.py
----------------------------
Regression test for log_utils._assert_known_client()'s division-suffix
handling.

adp_labor_distribution.py writes each payroll division under two different
client-string shapes for the same underlying client: append_payroll_log
gets the payroll_key form ("acme_agency", underscore, lowercase) while
append_digest_log gets the display-name form ("Acme Inc — Agency", em-dash,
title case). _assert_known_client only stripped the underscore form, so a
real, already-known client's Agency/Admin digest write was treated as an
"unrecognized client" and silently dropped from recon_log.json (falling
through to the interactive y/N prompt, which defaults to "no" under EOF in
a non-interactive run) -- caught live reconciling a real client's Labor
Distribution payroll: payroll_log.csv wrote fine, recon_log.json's digest
entry for both divisions silently failed with "Unrecognized client".

Uses the repo's own public clients/example_client.json ("Acme Inc",
payroll_key "acme") via _registry_test_utils -- no real client data needed,
this only tests the suffix-stripping/resolution logic itself.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests._registry_test_utils import install_example_registry, restore_registry
from log_utils import _assert_known_client


class AssertKnownClientDivisionSuffixTest(unittest.TestCase):
    def setUp(self):
        self._previous_registry = install_example_registry()

    def tearDown(self):
        restore_registry(self._previous_registry)

    def test_payroll_key_underscore_agency_suffix_resolves(self):
        _assert_known_client("acme_agency")  # must not raise / prompt

    def test_payroll_key_underscore_admin_suffix_resolves(self):
        _assert_known_client("acme_admin")

    def test_display_name_em_dash_agency_suffix_resolves(self):
        _assert_known_client("Acme Inc — Agency")

    def test_display_name_em_dash_admin_suffix_resolves(self):
        _assert_known_client("Acme Inc — Admin")

    def test_unrelated_unknown_client_still_rejected(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"BOOKKEEPING_NO_PROMPT": "1"}):
            with self.assertRaises(ValueError):
                _assert_known_client("Totally Made Up Client Zzz")


if __name__ == "__main__":
    unittest.main(verbosity=2)
