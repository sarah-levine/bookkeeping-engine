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


class CsvColumnOrderTest(unittest.TestCase):
    """Real bug: mark_clean.py hardcoded its own copy of the CSV column
    order with the last two columns swapped (run_timestamp before source,
    instead of source before run_timestamp) relative to log_utils.py's
    write_both_logs() -- the actual header every other writer produces.
    Confirmed live: running mark_clean.py once flipped the column order of
    every existing row in a real reconciliation_log.csv, and every
    Unicode em-dash in the accompanying recon_log.json got mangled to
    \\u2014 by the same run (see JsonUnicodeTest below) -- a whole-file
    diff for what should have been a single-row change.

    Fixed by having both modules import the same RECON_LOG_FIELDS
    constant from log_utils.py instead of each hardcoding their own copy."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.csv_path = Path(self.tmpdir.name) / "reconciliation_log.csv"
        self._orig_csv_path = mark_clean.CSV_PATH
        mark_clean.CSV_PATH = self.csv_path

    def tearDown(self):
        mark_clean.CSV_PATH = self._orig_csv_path
        self.tmpdir.cleanup()

    def test_header_matches_canonical_recon_log_fields(self):
        from log_utils import RECON_LOG_FIELDS
        entry = {
            "client": "TEST_CLIENT_XYZ", "account_type": "bofa_checking",
            "statement_end_date": "07/22/26", "beginning_balance": "100.00",
            "ending_balance": "200.00", "difference": "0.00",
        }
        mark_clean.update_csv(entry)
        with open(self.csv_path, newline="") as f:
            header = f.readline().strip().split(",")
        self.assertEqual(header, RECON_LOG_FIELDS)

    def test_source_column_precedes_run_timestamp(self):
        # Pin the specific real bug shape directly, not just equality with
        # the (also-fixable-in-tandem) constant.
        entry = {
            "client": "TEST_CLIENT_XYZ", "account_type": "bofa_checking",
            "statement_end_date": "07/22/26", "beginning_balance": "100.00",
            "ending_balance": "200.00", "difference": "0.00",
        }
        mark_clean.update_csv(entry)
        with open(self.csv_path, newline="") as f:
            header = f.readline().strip().split(",")
        self.assertLess(header.index("source"), header.index("run_timestamp"))

    def test_existing_rows_column_order_preserved_on_rewrite(self):
        # Write once, then update a second entry -- the first row's columns
        # must not flip order on the rewrite.
        mark_clean.update_csv({
            "client": "TEST_CLIENT_ONE", "account_type": "bofa_checking",
            "statement_end_date": "07/22/26", "beginning_balance": "1.00",
            "ending_balance": "2.00", "difference": "0.00",
        })
        with open(self.csv_path, newline="") as f:
            header_after_first_write = f.readline().strip().split(",")
        mark_clean.update_csv({
            "client": "TEST_CLIENT_TWO", "account_type": "chase_ink",
            "statement_end_date": "08/01/26", "beginning_balance": "3.00",
            "ending_balance": "4.00", "difference": "0.00",
        })
        with open(self.csv_path, newline="") as f:
            header_after_second_write = f.readline().strip().split(",")
        self.assertEqual(header_after_first_write, header_after_second_write)


class JsonUnicodeTest(unittest.TestCase):
    """Real bug: mark_clean.py's _save_log() called json.dump() without
    ensure_ascii=False, so every non-ASCII character already stored in
    recon_log.json (e.g. em-dashes in "CLIENT NAME — Admin" manual-
    issue entries) got escaped to a \\uXXXX sequence on every single run
    -- corrupting the whole file's readability for entries mark_clean.py
    never even touched. log_utils.py's own _save_log() already does this
    correctly; mark_clean.py had a second, separate, incorrect
    implementation."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.log_path = Path(self.tmpdir.name) / "recon_log.json"
        self._orig_log_path = mark_clean.LOG_PATH
        mark_clean.LOG_PATH = self.log_path

    def tearDown(self):
        mark_clean.LOG_PATH = self._orig_log_path
        self.tmpdir.cleanup()

    def test_unicode_preserved_not_escaped(self):
        entries = [{"client": "CONTOSO INC — Admin", "type": "manual",
                    "issue": "some note", "account_type": "", "statement_end_date": "",
                    "statement": "", "beginning_balance": "", "ending_balance": "",
                    "difference": "", "status": "", "issues": []}]
        mark_clean._save_log(entries)
        raw = self.log_path.read_text(encoding="utf-8")
        self.assertIn("—", raw)
        self.assertNotIn("\\u2014", raw)


if __name__ == "__main__":
    unittest.main()
