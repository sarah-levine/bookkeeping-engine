"""
test_payroll_end_to_end.py
---------------------------
Full-pipeline integration test for payroll: real fixture PDF -> real parse
functions -> the real _build_journal() for that format -> balance check
-> append_payroll_log() (into an isolated temp logs dir) -> read back via
payroll_log.csv / reconciliation_log.csv.

Unlike test_payroll.py (parser-only: no _build_journal call, no log
writes — see its own docstring), this exercises the exact code path that
produced two real bugs found 2026-07-02:

  1. adp_payroll_details.py's _build_journal silently dropped a "Sick"
     earnings category from gross wages — invisible to test_payroll.py
     because that test only checks raw parsed totals, never the
     constructed journal entry. The adp_payroll_details_jojo fixture
     below is the actual pay period that surfaced this bug (6/30/2026,
     $143.60 out of balance before the fix).
  2. append_payroll_log() never called update_sheet() at all, so every
     payroll run silently left the Google Sheet tracker stale — also
     invisible to test_payroll.py, which doesn't touch logging at all.

This test's balance-check assertion would have failed on bug #1, and its
update_sheet() call-recording assertion would have failed on bug #2.

Every configured fixture in PAYROLL_FIXTURES is checked (not just the
first one found) — each exercises a different format's _build_journal.

Requires local fixtures (source: 'repo' fixtures already in
Bookkeeping-clients/fixtures/) and each fixture's client config to have
payroll_key/payroll_format set. Skips cleanly without either.

Run:
    python3 tests/test_payroll_end_to_end.py
    python3 -m pytest tests/test_payroll_end_to_end.py -v
"""

import os
import sys
import csv
import tempfile
import shutil
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _run_adp_payroll_1099(pdf_path, cfg, liability_path=None):
    from payroll_clients.adp_payroll_1099 import parse_employees, _build_journal
    from payroll_clients.base import extract_text, parse_header

    text = extract_text(pdf_path)
    check_date = parse_header(text).get("check_date", "")
    employees = parse_employees(text)
    warnings = []
    rows = _build_journal(cfg, employees, check_date, warnings, pay_by_pay=0.0)
    return rows, check_date


def _run_adp_payroll_professional(pdf_path, cfg, liability_path=None):
    from payroll_clients.adp_payroll_professional import (
        parse_officers, parse_admin, parse_1099, parse_company_totals, _build_journal,
    )
    from payroll_clients.base import extract_text, parse_header

    text = extract_text(pdf_path)
    lines = text.split("\n")
    check_date = parse_header(text).get("check_date", "")
    officers = parse_officers(lines)
    admin = parse_admin(lines)
    total_1099 = parse_1099(lines)
    totals = parse_company_totals(lines)
    rows = _build_journal(cfg, officers, admin, total_1099, totals, check_date, pay_by_pay=0.0)
    return rows, check_date


def _run_adp_payroll_details(pdf_path, cfg, liability_path=None):
    from payroll_clients.adp_payroll_details import (
        parse_payroll_details, parse_liability, _build_journal,
    )

    data = parse_payroll_details(pdf_path, contractors_1099=cfg.get("contractors_1099"))
    wc_amount = 0.0
    if liability_path:
        wc_amount = parse_liability(liability_path)["wc"]
    rows = _build_journal(data, wc_amount, cfg)
    return rows, data["check_date"]


# format -> (pdf fixture filename, client config filename, runner)
# Add an entry here whenever a new format gets a real local fixture.
# "liability" is optional — only adp_payroll_details reads a separate
# Liability PDF (for workers comp).
PAYROLL_FIXTURES = [
    {
        "name":    "adp_payroll_1099_fcba",
        "pdf":     "fixture_adp_payroll_detail_fcba.pdf",
        "config":  "fcba_academy.json",
        "format":  "adp_payroll_1099",
        "runner":  _run_adp_payroll_1099,
    },
    {
        "name":    "adp_payroll_professional_mp_cheng",
        "pdf":     "fixture_adp_payroll_detail.pdf",
        "config":  "mp_cheng.json",
        "format":  "adp_payroll_professional",
        "runner":  _run_adp_payroll_professional,
    },
    {
        "name":       "adp_payroll_details_jojo",
        "pdf":        "fixture_adp_payroll_detail_jojo.pdf",
        "liability":  "fixture_adp_payroll_liability_jojo.pdf",
        "config":     "jojo_hair_studio.json",
        "format":     "adp_payroll_details",
        "runner":     _run_adp_payroll_details,
    },
]


def _clients_dir():
    from log_utils import get_clients_dir
    return get_clients_dir()


class _FixtureUnavailable(Exception):
    pass


def _resolve_entry(entry):
    """Return (pdf_path, liability_path_or_None, cfg), or raise _FixtureUnavailable."""
    from payroll_clients.base import load_config

    try:
        clients_dir = _clients_dir()
    except Exception as e:
        raise _FixtureUnavailable(f"clients dir unavailable: {e}")

    pdf_path = clients_dir / "fixtures" / entry["pdf"]
    cfg_path = clients_dir / entry["config"]
    if not pdf_path.exists() or not cfg_path.exists():
        raise _FixtureUnavailable(f"{entry['name']}: fixture or config not found locally")

    liability_path = None
    if entry.get("liability"):
        liab_path = clients_dir / "fixtures" / entry["liability"]
        if not liab_path.exists():
            raise _FixtureUnavailable(f"{entry['name']}: liability fixture not found locally")
        liability_path = str(liab_path)

    cfg = load_config(entry["config"])
    if not (cfg.get("payroll_key") and cfg.get("payroll_format")):
        raise _FixtureUnavailable(f"{entry['name']}: client config missing payroll_key/payroll_format")

    return str(pdf_path), liability_path, cfg


def check_payroll_fixture(entry) -> str:
    """Run one payroll fixture end-to-end. Returns a human-readable PASS
    line. Raises AssertionError on a real failure, _FixtureUnavailable if
    the fixture/config isn't set up locally."""
    from payroll_clients.base import check_balance, append_payroll_log
    import payroll_clients.base as pb

    pdf_path, liability_path, cfg = _resolve_entry(entry)
    rows, check_date = entry["runner"](pdf_path, cfg, liability_path)

    assert check_date, f"{entry['name']}: no check_date parsed"
    assert rows, f"{entry['name']}: _build_journal produced no rows"

    # The core regression check: a bug that silently drops a wage category
    # (like the "Sick" bug) throws this out of balance instead of posting
    # wrong numbers quietly.
    total_d, total_c = check_balance(rows)
    assert abs(total_d - total_c) < 0.02, (
        f"{entry['name']}: journal entry out of balance — "
        f"debits ${total_d:,.2f} vs credits ${total_c:,.2f} "
        f"(diff ${total_d - total_c:,.2f})"
    )

    # Record update_sheet() calls without needing real Google credentials —
    # the second regression check: append_payroll_log must actually call it.
    sheet_calls = []
    import sheets_updater as su
    real_update_sheet = su.update_sheet

    def _fake_update_sheet(client_key, account_type, date_str):
        sheet_calls.append((client_key, account_type, date_str))
        return True

    su.update_sheet = _fake_update_sheet

    tmp = Path(tempfile.mkdtemp())
    saved_payroll_path, saved_recon_path = pb.PAYROLL_LOG_PATH, pb.RECON_LOG_PATH
    try:
        # payroll_clients.base freezes its log paths at import time (unlike
        # log_utils.get_logs_dir(), which test_end_to_end.py can redirect
        # via an env var), so redirect the module attributes directly.
        pb.PAYROLL_LOG_PATH = tmp / "payroll_log.csv"
        pb.RECON_LOG_PATH = tmp / "reconciliation_log.csv"

        append_payroll_log(
            cfg.get("payroll_key") or cfg["client_name"],
            cfg["client_name"], check_date, rows,
        )

        # 1. payroll_log.csv got the row.
        assert pb.PAYROLL_LOG_PATH.exists(), "payroll_log.csv not written"
        with open(pb.PAYROLL_LOG_PATH, newline="") as f:
            payroll_rows = list(csv.DictReader(f))
        assert any(r["check_date"] == check_date for r in payroll_rows), (
            f"{entry['name']}: no payroll_log.csv row for check_date={check_date}"
        )

        # 2. reconciliation_log.csv got the payroll row (what the tracker
        #    date-lookup and the digest both read from).
        assert pb.RECON_LOG_PATH.exists(), "reconciliation_log.csv not written"
        with open(pb.RECON_LOG_PATH, newline="") as f:
            recon_rows = list(csv.DictReader(f))
        assert any(r["account_type"] == "payroll" for r in recon_rows), (
            f"{entry['name']}: no payroll row in reconciliation_log.csv"
        )

        # 3. update_sheet() was actually invoked — regression check for the
        #    "tracker silently never updates on a payroll run" bug.
        assert sheet_calls, (
            f"{entry['name']}: append_payroll_log() never called update_sheet() — "
            f"the Google Sheet tracker would silently stay stale"
        )
        assert sheet_calls[0][1] == "payroll", (
            f"{entry['name']}: update_sheet() called with account_type="
            f"{sheet_calls[0][1]!r}, expected 'payroll'"
        )

        return (f"PASS  {entry['name']:32s} format={entry['format']:26s} "
                f"balanced=${total_d:,.2f}  sheet_call={sheet_calls[0]}")
    finally:
        su.update_sheet = real_update_sheet
        pb.PAYROLL_LOG_PATH, pb.RECON_LOG_PATH = saved_payroll_path, saved_recon_path
        shutil.rmtree(tmp)


# ── pytest integration ────────────────────────────────────────────────────────
try:
    import pytest

    @pytest.mark.parametrize("entry", PAYROLL_FIXTURES, ids=lambda e: e["name"])
    def test_payroll_pdf_to_log_flow(entry):
        try:
            print(check_payroll_fixture(entry))
        except _FixtureUnavailable as e:
            pytest.skip(str(e))
except ImportError:
    pass


# ── plain-script runner ───────────────────────────────────────────────────────
def main():
    failures = skips = 0
    for entry in PAYROLL_FIXTURES:
        try:
            print(check_payroll_fixture(entry))
        except _FixtureUnavailable as e:
            skips += 1
            print(f"SKIP  {entry['name']}: {e}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {e}")
        except Exception as e:
            failures += 1
            print(f"ERROR {entry['name']}: {type(e).__name__}: {e}")

    summary = "All payroll fixtures passed." if not failures else f"{failures} failure(s)."
    if skips:
        summary += f" {skips} skipped."
    print(f"\n{summary}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
