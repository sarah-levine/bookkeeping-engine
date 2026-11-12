"""
test_cc_payment_issue_grouping.py
----------------------------------
Regression coverage for log_utils.append_cc_payment_issue(): consolidates
every "Unrecognized <Card> payment" occurrence for a client into a single
running list-style manual issue, instead of writing a new near-duplicate
full-sentence entry per occurrence.

Mode E's unrecognized-CC-payment check is session-scoped (see
REFACTORING_ROADMAP.md) and re-flags the same already-reconciled payment on
every later run that doesn't also reconcile that card's statement in the
same session. Before this change, each occurrence wrote its own
append_manual_issue() entry keyed on the full (differently-worded-only-by-
amount-and-date) sentence, so a single recurring false positive piled up
one near-duplicate entry per run -- confirmed live: Duran HCP accumulated
7+ separate "Unrecognized Amex payment ..." entries this way.
"""
import json
import sys
import tempfile
import shutil
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import log_utils
from parsers.base import ClientRegistry
import parsers.base as _parsers_base


class AppendCcPaymentIssueTest(unittest.TestCase):

    def setUp(self):
        self.clients_dir = Path(tempfile.mkdtemp())
        self.logs_dir = Path(tempfile.mkdtemp())
        with open(self.clients_dir / "test_client.json", "w") as f:
            json.dump({
                "client_name":    "Test Salon LLC",
                "canonical_name": "TEST SALON LLC",
                "aliases":        [],
                "statement_types": [],
            }, f)

        self._orig_registry = _parsers_base._registry
        _parsers_base._registry = ClientRegistry(clients_dir=str(self.clients_dir))

        self._orig_get_logs_dir = log_utils.get_logs_dir
        log_utils.get_logs_dir = lambda: self.logs_dir

    def tearDown(self):
        _parsers_base._registry = self._orig_registry
        log_utils.get_logs_dir = self._orig_get_logs_dir
        shutil.rmtree(self.clients_dir)
        shutil.rmtree(self.logs_dir)

    def _manual_entries(self):
        with open(self.logs_dir / "recon_log.json") as f:
            entries = json.load(f)
        return [e for e in entries if e.get("type") == "manual"]

    def test_first_occurrence_creates_one_entry(self):
        log_utils.append_cc_payment_issue(
            client="Test Salon LLC", card="Amex", amount_str="$3,900.00", date="06/29/26")
        entries = self._manual_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["items"], [{"amount": "$3,900.00", "date": "06/29/26"}])
        self.assertIn("$3,900.00", entries[0]["issue"])
        self.assertIn("06/29/26", entries[0]["issue"])

    def test_second_distinct_occurrence_appends_to_same_entry(self):
        log_utils.append_cc_payment_issue(
            client="Test Salon LLC", card="Amex", amount_str="$3,900.00", date="06/29/26")
        log_utils.append_cc_payment_issue(
            client="Test Salon LLC", card="Amex", amount_str="$2,000.00", date="04/03/26")
        entries = self._manual_entries()
        # Still a single entry, not two -- the list grew instead of a new
        # near-duplicate full-sentence entry being written.
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(entries[0]["items"]), 2)
        # Sorted chronologically regardless of append order.
        self.assertEqual(entries[0]["items"][0]["date"], "04/03/26")
        self.assertEqual(entries[0]["items"][1]["date"], "06/29/26")

    def test_duplicate_occurrence_is_idempotent(self):
        log_utils.append_cc_payment_issue(
            client="Test Salon LLC", card="Amex", amount_str="$3,900.00", date="06/29/26")
        log_utils.append_cc_payment_issue(
            client="Test Salon LLC", card="Amex", amount_str="$3,900.00", date="06/29/26")
        entries = self._manual_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(entries[0]["items"]), 1)

    def test_resolved_entry_stays_resolved_on_duplicate_reappend(self):
        log_utils.append_cc_payment_issue(
            client="Test Salon LLC", card="Amex", amount_str="$3,900.00", date="06/29/26")
        log_utils.resolve_manual_issue(
            client="Test Salon LLC", issue=self._manual_entries()[0]["issue"])
        self.assertTrue(self._manual_entries()[0]["resolved"])

        # Re-detecting the exact same already-known item must not reopen it.
        log_utils.append_cc_payment_issue(
            client="Test Salon LLC", card="Amex", amount_str="$3,900.00", date="06/29/26")
        self.assertTrue(self._manual_entries()[0]["resolved"])

    def test_resolved_entry_reopens_on_genuinely_new_item(self):
        log_utils.append_cc_payment_issue(
            client="Test Salon LLC", card="Amex", amount_str="$3,900.00", date="06/29/26")
        log_utils.resolve_manual_issue(
            client="Test Salon LLC", issue=self._manual_entries()[0]["issue"])
        self.assertTrue(self._manual_entries()[0]["resolved"])

        # A genuinely new occurrence is new, unaddressed information --
        # the note must come back as unresolved.
        log_utils.append_cc_payment_issue(
            client="Test Salon LLC", card="Amex", amount_str="$2,000.00", date="08/15/26")
        entries = self._manual_entries()
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0].get("resolved", False))
        self.assertEqual(len(entries[0]["items"]), 2)

    def test_different_cards_get_separate_entries(self):
        log_utils.append_cc_payment_issue(
            client="Test Salon LLC", card="Amex", amount_str="$3,900.00", date="06/29/26")
        log_utils.append_cc_payment_issue(
            client="Test Salon LLC", card="Chase", amount_str="$500.00", date="07/01/26")
        entries = self._manual_entries()
        self.assertEqual(len(entries), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
