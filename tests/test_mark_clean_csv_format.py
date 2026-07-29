"""
test_mark_clean_csv_format.py
------------------------------
Regression coverage for mark_clean.py's reconciliation_log.csv date format.

Bug this guards against: write_both_logs() (log_utils.py) normalizes
statement_date to ISO (YYYY-MM-DD) before writing to reconciliation_log.csv,
explicitly "for consistent ISO-sortable storage." mark_clean.py's own
update_csv() wrote the same column using MM/DD/YY instead — a second write
path silently disagreeing with the first's documented convention for the
same field.

send_morning_digest.py's load_reconciliation_log() dedups by the most
recent statement_date per (client, account_type) using a plain string
comparison. Mixed formats broke that: "2026-06-22" (ISO) sorts ahead of
"07/22/26" (MM/DD/YY) lexicographically ('2' > '0'), even though July is
chronologically later — so a real, newly-reconciled July statement was
invisible to the tracker/overdue email, silently reverting to a stale June
date. Confirmed live against real production data.

Uses a temp CSV file — no real client data, no network.
"""
import csv
import tempfile
import unittest
from pathlib import Path

import mark_clean


class UpdateCsvDateFormatTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.csv_path = Path(self.tmpdir.name) / "reconciliation_log.csv"
        self._orig_csv_path = mark_clean.CSV_PATH
        mark_clean.CSV_PATH = self.csv_path

    def tearDown(self):
        mark_clean.CSV_PATH = self._orig_csv_path
        self.tmpdir.cleanup()

    def _rows(self):
        with open(self.csv_path, newline="") as f:
            return list(csv.DictReader(f))

    def test_writes_iso_date_not_mm_dd_yy(self):
        entry = {
            "client": "TEST_CLIENT_XYZ", "account_type": "bofa_checking",
            "statement_end_date": "07/22/26", "beginning_balance": "100.00",
            "ending_balance": "200.00", "difference": "0.00",
        }
        mark_clean.update_csv(entry)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["statement_date"], "2026-07-22")

    def test_accepts_already_iso_input_unchanged(self):
        entry = {
            "client": "TEST_CLIENT_XYZ", "account_type": "bofa_checking",
            "statement_end_date": "2026-07-22", "beginning_balance": "100.00",
            "ending_balance": "200.00", "difference": "0.00",
        }
        mark_clean.update_csv(entry)
        self.assertEqual(self._rows()[0]["statement_date"], "2026-07-22")

    def test_matches_write_both_logs_format_for_same_column(self):
        # write_both_logs() (log_utils.py) normalizes via the same
        # _normalize_date_iso() helper — this just pins that both paths
        # agree on the literal output for a representative input, so a
        # future change to one in isolation would fail loudly here too.
        from log_utils import _normalize_date_iso
        entry = {
            "client": "TEST_CLIENT_XYZ", "account_type": "chase_ink",
            "statement_end_date": "07/22/26", "beginning_balance": "1.00",
            "ending_balance": "2.00", "difference": "0.00",
        }
        mark_clean.update_csv(entry)
        self.assertEqual(self._rows()[0]["statement_date"],
                          _normalize_date_iso("07/22/26"))


if __name__ == "__main__":
    unittest.main()
