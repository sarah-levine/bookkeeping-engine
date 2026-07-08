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
     constructed journal entry. One fixture's real pay period is the
     actual case that surfaced this bug ($143.60 out of balance before
     the fix).
  2. append_payroll_log() never called update_sheet() at all, so every
     payroll run silently left the Google Sheet tracker stale — also
     invisible to test_payroll.py, which doesn't touch logging at all.

This test's balance-check assertion would have failed on bug #1, and its
update_sheet() call-recording assertion would have failed on bug #2.

Every configured fixture in the manifest is checked (not just the first
one found) — each exercises a different format's _build_journal.

Requires payroll_fixtures_manifest.json (gitignored — copy
payroll_fixtures_manifest.example.json and fill in real client/fixture
filenames from the private Bookkeeping-clients repo) and each fixture's
client config to have payroll_key/payroll_format set. Skips cleanly
without either.

Run:
    python3 tests/test_payroll_end_to_end.py
    python3 -m pytest tests/test_payroll_end_to_end.py -v
"""

import os
import sys
import csv
import json
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


def _run_adp_payroll_tipped(pdf_path, cfg, liability_path=None):
    from payroll_clients.adp_payroll_tipped import (
        parse_header, parse_officers, parse_support, parse_company_totals,
        parse_1099, _build_journal,
    )
    from payroll_clients.base import extract_text

    text = extract_text(pdf_path)
    lines = text.split("\n")
    check_date = parse_header(text).get("check_date", "")
    officers = parse_officers(lines)
    support = parse_support(lines)
    company = parse_company_totals(text)
    total_1099 = parse_1099(lines)
    rows = _build_journal(cfg, officers, support, company, total_1099, check_date)
    return rows, check_date


def _run_adp_payroll_departments(pdf_path, cfg, liability_path=None):
    """adp_payroll_departments has no separate _build_journal() (unlike the
    other formats above) — its rows are built inline in
    run_adp_payroll_departments(). Rather than reimplementing that logic
    here, call the real function with _qb_confirm/append_payroll_log/
    append_digest_log/archive_payroll_pdf/load_config monkeypatched to
    capture args instead of prompting/writing/uploading."""
    import payroll_clients.adp_payroll_departments as mod

    captured = {}

    def _fake_append_payroll_log(client, client_name, check_date, rows, **kw):
        captured["rows"] = rows
        captured["check_date"] = check_date

    orig = {
        "_qb_confirm": mod._qb_confirm,
        "append_payroll_log": mod.append_payroll_log,
        "append_digest_log": mod.append_digest_log,
        "archive_payroll_pdf": mod.archive_payroll_pdf,
        "load_config": mod.load_config,
    }
    mod._qb_confirm = lambda label: True
    mod.append_payroll_log = _fake_append_payroll_log
    mod.append_digest_log = lambda *a, **k: None
    mod.archive_payroll_pdf = lambda *a, **k: None
    mod.load_config = lambda _: cfg
    try:
        mod.run_adp_payroll_departments([pdf_path, liability_path], "unused.json")
    finally:
        for name, val in orig.items():
            setattr(mod, name, val)

    if "rows" not in captured:
        raise AssertionError(
            "run_adp_payroll_departments did not call append_payroll_log "
            "(did _qb_confirm return False?)"
        )
    return captured["rows"], captured["check_date"]


def _run_adp_labor_distribution_calls(pdf_path, cfg):
    """Calls the real run_adp_labor_distribution() once, capturing BOTH the
    Agency (Div 50) and Admin (Div 10) append_payroll_log() calls it makes
    internally — same monkeypatch technique as _run_adp_payroll_departments,
    for the same reason (no separate _build_journal() to call for each
    division; row-building is inline)."""
    import payroll_clients.adp_labor_distribution as mod

    calls = []

    def _fake_append_payroll_log(client, client_name, check_date, rows, **kw):
        calls.append({"client": client, "check_date": check_date, "rows": rows})

    orig = {
        "_qb_confirm": mod._qb_confirm,
        "append_payroll_log": mod.append_payroll_log,
        "append_digest_log": mod.append_digest_log,
        "archive_payroll_pdf": mod.archive_payroll_pdf,
        "load_config": mod.load_config,
    }
    mod._qb_confirm = lambda label: True
    mod.append_payroll_log = _fake_append_payroll_log
    mod.append_digest_log = lambda *a, **k: None
    mod.archive_payroll_pdf = lambda *a, **k: None
    mod.load_config = lambda _: cfg
    try:
        mod.run_adp_labor_distribution([pdf_path], "unused.json")
    finally:
        for name, val in orig.items():
            setattr(mod, name, val)

    return calls


def _run_adp_labor_distribution_agency(pdf_path, cfg, liability_path=None):
    calls = _run_adp_labor_distribution_calls(pdf_path, cfg)
    agency = next((c for c in calls if c["client"].endswith("_agency")), None)
    if agency is None:
        raise AssertionError(
            "run_adp_labor_distribution did not log the Agency (Div 50) journal "
            "(did _qb_confirm return False for it?)"
        )
    return agency["rows"], agency["check_date"]


def _run_square_payroll(pdf_path, cfg, liability_path=None):
    from payroll_clients.square_payroll import parse_workbook, _build_journal

    parsed = parse_workbook(pdf_path)
    rows = _build_journal(cfg, parsed, parsed["check_date"])
    return rows, parsed["check_date"]


def _run_adp_labor_distribution_admin(pdf_path, cfg, liability_path=None):
    calls = _run_adp_labor_distribution_calls(pdf_path, cfg)
    admin = next((c for c in calls if c["client"].endswith("_admin")), None)
    if admin is None:
        raise AssertionError(
            "run_adp_labor_distribution did not log the Admin (Div 10) journal "
            "(did _qb_confirm return False for it?)"
        )
    return admin["rows"], admin["check_date"]


# format -> runner. The actual client/fixture filenames (real client data)
# live in payroll_fixtures_manifest.json (gitignored), not here — see
# payroll_fixtures_manifest.example.json for the schema.
RUNNER_BY_FORMAT = {
    "adp_payroll_1099":            _run_adp_payroll_1099,
    "adp_payroll_professional":    _run_adp_payroll_professional,
    "adp_payroll_details":         _run_adp_payroll_details,
    "adp_payroll_tipped":          _run_adp_payroll_tipped,
    "adp_payroll_departments":     _run_adp_payroll_departments,
    "adp_labor_distribution_agency": _run_adp_labor_distribution_agency,
    "adp_labor_distribution_admin":  _run_adp_labor_distribution_admin,
    "square_payroll":              _run_square_payroll,
}

_DIR = Path(__file__).parent


def load_manifest():
    """Prefer the real (gitignored) manifest; fall back to the example."""
    real = _DIR / "payroll_fixtures_manifest.json"
    path = real if real.exists() else _DIR / "payroll_fixtures_manifest.example.json"
    with open(path) as f:
        return json.load(f), path


def _payroll_fixtures():
    """Configured fixtures with a runner for their format, real filenames
    filled in (skips REPLACE_ME placeholders from the example manifest)."""
    manifest, path = load_manifest()
    using_example = path.name.endswith(".example.json")
    out = []
    for entry in manifest.get("fixtures", []):
        if using_example or entry.get("pdf", "REPLACE_ME.pdf").startswith("REPLACE_ME"):
            continue
        runner = RUNNER_BY_FORMAT.get(entry["format"])
        if runner:
            out.append({**entry, "runner": runner})
    return out


PAYROLL_FIXTURES = _payroll_fixtures()


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

    @pytest.mark.skipif(not PAYROLL_FIXTURES,
                        reason="no configured fixtures (copy payroll_fixtures_manifest.example.json → payroll_fixtures_manifest.json)")
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
