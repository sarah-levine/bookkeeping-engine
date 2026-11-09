"""
test_detect_client_account_ending.py
--------------------------------------
Regression coverage for StatementParser._detect_client()'s "Account Ending
In" fallback (parsers/base.py), used when no known client name/alias
appears anywhere in the statement text (e.g. a Capital One card issued to
an individual's name rather than the company name).

The original regex required a literal dotted leader between "IN" and the
digits (`ACCOUNT ENDING IN\\s+\\.+(\\d{4})`), matching a dotted print-coupon
format like "Account Ending In ....7440". A real Capital One statement's
own body text instead reads "ACCOUNT ENDING IN 7440" with a plain space
and no dots at all -- the old regex silently failed to match it, leaving
client_name unset ("Client not recognized") even though the account
ending WAS present and registered in that client's config. Caught live
reconciling a real Duran HCP Capital One statement.
"""
import unittest
from unittest.mock import patch

import parsers.base as base_mod
from parsers.base import StatementParser


class DetectClientAccountEndingTest(unittest.TestCase):
    def _parser(self, text):
        p = StatementParser.__new__(StatementParser)
        p.text = text
        return p

    def test_plain_space_separator_resolves(self):
        # Real-world shape: no dots at all between "IN" and the digits.
        text = "SOME CARD\nPAYMENT DUE DATE: AUG 02, 2026 ACCOUNT ENDING IN 7440\n"
        p = self._parser(text)
        with patch.object(base_mod._registry, 'lookup_account_ending',
                           return_value=("BRAVO STUDIO LLC", "capital_one")) as mock_lookup:
            result = p._detect_client()
        mock_lookup.assert_called_once_with("7440")
        self.assertEqual(result, "BRAVO STUDIO LLC")

    def test_dotted_leader_separator_still_resolves(self):
        # Original print-coupon shape must keep working after the fix.
        text = "Account Ending in ....7440\n"
        p = self._parser(text)
        with patch.object(base_mod._registry, 'lookup_account_ending',
                           return_value=("BRAVO STUDIO LLC", "capital_one")) as mock_lookup:
            result = p._detect_client()
        mock_lookup.assert_called_once_with("7440")
        self.assertEqual(result, "BRAVO STUDIO LLC")

    def test_no_match_in_registry_returns_none(self):
        text = "ACCOUNT ENDING IN 0000\n"
        p = self._parser(text)
        with patch.object(base_mod._registry, 'lookup_account_ending', return_value=None):
            result = p._detect_client()
        self.assertIsNone(result)

    def test_no_account_ending_text_at_all_returns_none(self):
        p = self._parser("Nothing relevant here.\n")
        with patch.object(base_mod._registry, 'lookup_account_ending') as mock_lookup:
            result = p._detect_client()
        mock_lookup.assert_not_called()
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
