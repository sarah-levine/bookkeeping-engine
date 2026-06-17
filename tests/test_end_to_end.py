"""
test_end_to_end.py
------------------
Full-pipeline integration tests that chain the real components together:

  PDF → detect_statement_type → parser → generate_report
      → write_both_logs (into a temp logs dir)
      → load_recon_log / digest tracker render

Two tests:
  1. test_router_matches_manifest — detect_statement_type() agrees with the
     declared format for every configured bank fixture (catches auto-router
     regressions across all formats).
  2. test_pdf_to_digest_flow — takes one bank fixture all the way through:
     detect → parse → report → write logs → read them back via load_recon_log
     and the digest's reconciliation reader.

Requires fixture PDFs (fixtures_manifest.json) and pdfplumber, so it runs on a
machine with the private fixtures and skips cleanly elsewhere (e.g. CI).

Run:
    python3 tests/test_end_to_end.py
    python3 -m pytest tests/test_end_to_end.py -v
"""

import os
import sys
import csv
import tempfile
import shutil
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.drive_fixtures import fetch_pdf_entry, DriveUnavailable  # noqa: E402
from tests.test_parsers import PARSER_MAP, _iter_real_entries        # noqa: E402


def _bank_fixtures():
    """Configured fixtures whose format maps to a real bank parser."""
    out = []
    for entry, _ in _iter_real_entries():
        if entry.get("format") in PARSER_MAP:
            out.append(entry)
    return out


def _balance_of(parser):
    for attr in ("ending_balance", "new_balance", "beginning_balance", "previous_balance"):
        v = getattr(parser, attr, None)
        if v is not None:
            return v
    return None


def test_router_matches_manifest():
    """split_combined_pdf() must route each fixture to its declared format.

    Uses split_combined_pdf (the real reconciliation entry point) rather than
    detect_statement_type, because one PDF can legitimately map to several
    account types — e.g. a bundled CitiBusiness statement reconciled as both
    checking and savings. The manifest format must be among the routed types.
    """
    try:
        from reconcile_comprehensive import split_combined_pdf
    except ImportError as e:
        raise unittest.SkipTest(f"reconcile_comprehensive unavailable: {e}")

    entries = _bank_fixtures()
    if not entries:
        raise unittest.SkipTest("no bank fixtures configured")

    checked = 0
    for entry in entries:
        try:
            pdf = fetch_pdf_entry(entry, cache_name=f"{entry['name']}.pdf")
        except DriveUnavailable:
            continue
        try:
            routed = [t for t, _ in split_combined_pdf(str(pdf))]
        except Exception as e:
            raise AssertionError(f"{entry['name']}: split_combined_pdf raised {type(e).__name__}: {e}")
        assert entry["format"] in routed, \
            f"{entry['name']}: router gave {routed!r}, manifest says {entry['format']!r}"
        checked += 1

    if checked == 0:
        raise unittest.SkipTest("bank fixtures configured but none fetchable")
    print(f"PASS  test_router_matches_manifest ({checked} fixtures routed correctly)")


def test_pdf_to_digest_flow():
    """One fixture end-to-end: PDF → parse → report → write logs → read back."""
    try:
        from reconcile_comprehensive import detect_statement_type
        import log_utils
        from log_utils import write_both_logs, load_recon_log, _normalize_client_key
    except ImportError as e:
        raise unittest.SkipTest(f"pipeline modules unavailable: {e}")

    entries = _bank_fixtures()
    if not entries:
        raise unittest.SkipTest("no bank fixtures configured")

    tmp = Path(tempfile.mkdtemp())
    saved_env = os.environ.get("BOOKKEEPING_LOGS_DIR")
    try:
        # Route all log writes/reads to an isolated temp dir.
        os.environ["BOOKKEEPING_LOGS_DIR"] = str(tmp)
        assert log_utils.get_logs_dir() == tmp

        # Find the first fixture that parses with a balance.
        chosen = report = parser = stmt_type = None
        for entry in entries:
            try:
                pdf = fetch_pdf_entry(entry, cache_name=f"{entry['name']}.pdf")
            except DriveUnavailable:
                continue
            stmt_type = detect_statement_type(str(pdf))
            parser_cls = PARSER_MAP.get(stmt_type)
            if not parser_cls:
                continue
            parser = parser_cls(str(pdf))
            parser.parse()
            if _balance_of(parser) is not None:
                report = parser.generate_report()
                chosen = entry
                break

        if not chosen:
            raise unittest.SkipTest("no fetchable fixture parsed with a balance")

        assert report and "BALANCE" in report.upper(), "report missing balance section"

        client      = parser.client_name or "Example Client Inc"
        bal         = _balance_of(parser)
        stmt_date   = getattr(parser, "closing_date", None) or "05/31/26"

        # Write through the real reconciliation log writer.
        write_both_logs(
            client=client,
            client_name=client,
            account_type=stmt_type,
            statement_end_date=str(stmt_date),
            statement=chosen["name"],
            beginning_balance="",
            ending_balance=f"{bal}",
            total_payments="",
            status="CLEAN",
        )

        # 1. recon_log.json round-trips via load_recon_log (today's run).
        today = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
        recon, _ = load_recon_log(today)
        match = [e for e in recon if e.get("account_type") == stmt_type]
        assert match, f"written {stmt_type} entry not found in recon_log via load_recon_log"

        # 2. reconciliation_log.csv was written to the temp logs dir.
        csv_path = tmp / "reconciliation_log.csv"
        assert csv_path.exists(), "reconciliation_log.csv not written to logs dir"
        with open(csv_path, newline="") as f:
            rows = list(csv.DictReader(f))
        key = _normalize_client_key(client)
        csv_match = [r for r in rows if r["client"] == key and r["account_type"] == stmt_type]
        assert csv_match, f"row for {key}/{stmt_type} not in reconciliation_log.csv"

        # 3. The digest's reconciliation reader sees the row.
        import send_morning_digest as digest
        digest.LOG_DIR = tmp  # point the digest at the temp logs dir
        latest = digest.load_reconciliation_log()
        assert (key, stmt_type) in latest, \
            f"digest reader missing ({key}, {stmt_type}); has {list(latest)[:5]}..."

        print(f"PASS  test_pdf_to_digest_flow  ({chosen['name']} → {stmt_type} → "
              f"logs → digest, client={client!r})")
    finally:
        if saved_env is None:
            os.environ.pop("BOOKKEEPING_LOGS_DIR", None)
        else:
            os.environ["BOOKKEEPING_LOGS_DIR"] = saved_env
        shutil.rmtree(tmp)


# ── pytest integration ────────────────────────────────────────────────────────
try:
    import pytest

    def test_router_matches_manifest_pytest():
        try:
            test_router_matches_manifest()
        except unittest.SkipTest as e:
            pytest.skip(str(e))

    def test_pdf_to_digest_flow_pytest():
        try:
            test_pdf_to_digest_flow()
        except unittest.SkipTest as e:
            pytest.skip(str(e))
except ImportError:
    pass


# ── runner ────────────────────────────────────────────────────────────────────

TESTS = [test_router_matches_manifest, test_pdf_to_digest_flow]


def main():
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
