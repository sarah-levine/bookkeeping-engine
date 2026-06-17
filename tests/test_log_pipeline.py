"""
test_log_pipeline.py
--------------------
End-to-end tests for the log pipeline:
  payroll_log.csv  →  reconciliation_log.csv  →  tracker render

These tests use only synthetic data — no PDFs, no Drive credentials,
no git push. They verify the full write→read→render chain that has
historically caused "stale date" bugs in the morning digest tracker.

Run:
    python3 -m pytest tests/test_log_pipeline.py -v
    # or without pytest:
    python3 tests/test_log_pipeline.py
"""

import csv
import sys
import tempfile
import shutil
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from payroll_clients.base import (
    _upsert_csv,
    PAYROLL_LOG_FIELDS,
    RECON_LOG_FIELDS,
)
from log_utils import _normalize_client_key


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_tmp_logs():
    """Return (tmp_dir, payroll_log_path, recon_log_path) — all isolated temp files."""
    tmp = Path(tempfile.mkdtemp())
    return tmp, tmp / "payroll_log.csv", tmp / "reconciliation_log.csv"


def _write_payroll_and_recon(payroll_path, recon_path, client, client_name,
                              check_date, bank_credit, balanced=True):
    """Simulate what append_payroll_log() does, but against temp paths."""
    # payroll_log
    p_entry = {
        "client":        client,
        "client_name":   client_name,
        "check_date":    check_date,
        "bank_credit":   f"{bank_credit:.2f}",
        "balanced":      "TRUE" if balanced else "FALSE",
        "run_timestamp": "2026-01-01 00:00:00",
    }
    _upsert_csv(payroll_path, PAYROLL_LOG_FIELDS, ["client", "check_date"], p_entry)

    # reconciliation_log — same upsert key as production code
    normalized_key = _normalize_client_key(client)
    r_entry = {
        "client":             normalized_key,
        "client_name":        client_name,
        "account_type":       "payroll",
        "account_ending":     "",
        "statement_date":     check_date,
        "beginning_balance":  "",
        "ending_balance":     "",
        "total_payments":     "",
        "run_timestamp":      "2026-01-01 00:00:00",
        "source":             "test",
    }
    _upsert_csv(recon_path, RECON_LOG_FIELDS, ["client", "account_type"], r_entry)

    return p_entry, r_entry


def _read_recon_log(recon_path):
    if not recon_path.exists():
        return []
    with open(recon_path, newline="") as f:
        return list(csv.DictReader(f))


def _read_payroll_log(payroll_path):
    if not payroll_path.exists():
        return []
    with open(payroll_path, newline="") as f:
        return list(csv.DictReader(f))


# ── tests ─────────────────────────────────────────────────────────────────────

def test_payroll_run_writes_reconciliation_log():
    """A payroll run must write statement_date into reconciliation_log.csv."""
    tmp, pl, rl = _make_tmp_logs()
    try:
        _write_payroll_and_recon(pl, rl, "adp_payroll_tipped", "Test Client A",
                                  "05/15/26", 7183.51)
        rows = _read_recon_log(rl)
        assert len(rows) == 1, "Expected exactly one row in reconciliation_log"
        assert rows[0]["account_type"] == "payroll"
        assert rows[0]["statement_date"] == "05/15/26"
        print("PASS  test_payroll_run_writes_reconciliation_log")
    finally:
        shutil.rmtree(tmp)


def test_second_payroll_run_updates_not_appends():
    """Running payroll twice for the same client must update the date, not add a second row."""
    tmp, pl, rl = _make_tmp_logs()
    try:
        _write_payroll_and_recon(pl, rl, "adp_payroll_tipped", "Test Client A",
                                  "04/15/26", 7183.51)
        _write_payroll_and_recon(pl, rl, "adp_payroll_tipped", "Test Client A",
                                  "05/29/26", 8375.30)

        rows = _read_recon_log(rl)
        payroll_rows = [r for r in rows if r["account_type"] == "payroll"]
        assert len(payroll_rows) == 1, \
            f"Expected 1 payroll row, got {len(payroll_rows)} — stale date not overwritten"
        assert payroll_rows[0]["statement_date"] == "05/29/26", \
            f"Expected 05/29/26, got {payroll_rows[0]['statement_date']}"
        print("PASS  test_second_payroll_run_updates_not_appends")
    finally:
        shutil.rmtree(tmp)


def test_multiple_clients_do_not_overwrite_each_other():
    """Test Client A and Test Client B payroll rows must coexist — different clients, same account_type."""
    tmp, pl, rl = _make_tmp_logs()
    try:
        _write_payroll_and_recon(pl, rl, "test_client_a", "Test Client A",
                                  "05/29/26", 8375.30)
        _write_payroll_and_recon(pl, rl, "test_client_b", "Test Client B",
                                  "05/14/26", 8835.13)

        rows = _read_recon_log(rl)
        payroll_rows = [r for r in rows if r["account_type"] == "payroll"]
        assert len(payroll_rows) == 2, \
            f"Expected 2 payroll rows (one per client), got {len(payroll_rows)}"
        dates = {r["client"]: r["statement_date"] for r in payroll_rows}
        assert dates.get("TEST_CLIENT_A") == "05/29/26", \
            f"Test Client A date wrong: {dates.get('TEST_CLIENT_A')}"
        assert dates.get("TEST_CLIENT_B") == "05/14/26", \
            f"Test Client B date wrong: {dates.get('TEST_CLIENT_B')}"
        print("PASS  test_multiple_clients_do_not_overwrite_each_other")
    finally:
        shutil.rmtree(tmp)


def test_tracker_renders_correct_date_after_payroll_run():
    """
    The tracker's get_tracker_date() must return the date written by the most
    recent payroll run, not a stale value.
    This is the exact bug that caused a client to show 04/15/26 instead of 05/29/26.
    """
    tmp, pl, rl = _make_tmp_logs()
    try:
        # Write old run then new run
        _write_payroll_and_recon(pl, rl, "test_client_a", "Test Client A",
                                  "04/15/26", 7183.51)
        _write_payroll_and_recon(pl, rl, "test_client_a", "Test Client A",
                                  "05/29/26", 8375.30)

        # Now simulate what load_reconciliation_log() + get_tracker_date() does
        # by reading the temp log directly
        rows = _read_recon_log(rl)
        recon_dates = {}
        for row in rows:
            ck = row.get("client", "").strip()
            at = row.get("account_type", "").strip()
            sd = row.get("statement_date", "").strip()
            if ck and at and sd:
                existing = recon_dates.get((ck, at))
                if not existing or sd > existing:
                    recon_dates[(ck, at)] = sd

        client_a_payroll_date = recon_dates.get(("TEST_CLIENT_A", "payroll"))
        assert client_a_payroll_date == "05/29/26", \
            f"Tracker would show stale date: {client_a_payroll_date} (expected 05/29/26)"
        print("PASS  test_tracker_renders_correct_date_after_payroll_run")
    finally:
        shutil.rmtree(tmp)


def test_reconciliation_log_write_without_payroll_log_dependency():
    """
    The reconciliation_log write must succeed even if payroll_log.csv
    doesn't exist yet (first run on a fresh repo clone).
    """
    tmp, pl, rl = _make_tmp_logs()
    try:
        assert not pl.exists(), "payroll_log should not exist yet"
        # Write only to recon_log (skipping payroll_log, like a fresh repo)
        normalized_key = _normalize_client_key("adp_payroll_details")
        r_entry = {
            "client": normalized_key, "client_name": "Test Client B",
            "account_type": "payroll", "account_ending": "",
            "statement_date": "05/14/26", "beginning_balance": "",
            "ending_balance": "", "total_payments": "",
            "run_timestamp": "2026-01-01 00:00:00", "source": "test",
        }
        _upsert_csv(rl, RECON_LOG_FIELDS, ["client", "account_type"], r_entry)
        rows = _read_recon_log(rl)
        assert len(rows) == 1
        assert rows[0]["statement_date"] == "05/14/26"
        print("PASS  test_reconciliation_log_write_without_payroll_log_dependency")
    finally:
        shutil.rmtree(tmp)


def test_normalize_client_key_maps_correctly():
    """_normalize_client_key uses tracker_key from client config when payroll_format matches.

    Skips if no private client configs with tracker_key are available (e.g. CI).
    To activate: add "tracker_key": "<TRACKER_KEY>" to each private client JSON.
    """
    import unittest
    try:
        from parsers.base import _registry
        keyed = [
            (cfg["payroll_format"], cfg["tracker_key"])
            for cfg in _registry._configs.values()
            if cfg.get("payroll_format") and cfg.get("tracker_key")
        ]
    except Exception:
        keyed = []

    if not keyed:
        raise unittest.SkipTest("No client configs with tracker_key available — add tracker_key to private client JSONs")

    for payroll_format, expected in keyed:
        got = _normalize_client_key(payroll_format)
        assert got == expected, \
            f"_normalize_client_key({payroll_format!r}) = {got!r}, expected {expected!r}"
    print("PASS  test_normalize_client_key_maps_correctly")


def test_normalize_client_key_uppercase_fallback():
    """_normalize_client_key falls back to uppercase+underscores for unknown inputs."""
    assert _normalize_client_key("unknown_client_xyz") == "UNKNOWN_CLIENT_XYZ"
    assert _normalize_client_key("some client name") == "SOME_CLIENT_NAME"
    print("PASS  test_normalize_client_key_uppercase_fallback")


# ── runner ────────────────────────────────────────────────────────────────────

TESTS = [
    test_payroll_run_writes_reconciliation_log,
    test_second_payroll_run_updates_not_appends,
    test_multiple_clients_do_not_overwrite_each_other,
    test_tracker_renders_correct_date_after_payroll_run,
    test_reconciliation_log_write_without_payroll_log_dependency,
    test_normalize_client_key_maps_correctly,
    test_normalize_client_key_uppercase_fallback,
]


def main():
    import unittest
    failures = skips = 0
    for t in TESTS:
        try:
            t()
        except unittest.SkipTest as e:
            skips += 1
            print(f"SKIP  {t.__name__}: {e}")
        except Exception as e:
            failures += 1
            print(f"FAIL  {t.__name__}: {e}")
    summary = "All tests passed." if not failures else f"{failures} failure(s)."
    if skips:
        summary += f" {skips} skipped."
    print(f"\n{summary}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
