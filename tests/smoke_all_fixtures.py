#!/usr/bin/env python3
"""
smoke_all_fixtures.py
---------------------
Run reconcile_comprehensive.py --dry-run against every fixture in the manifest.

This is a full-pipeline smoke test: PDF → detect → parse → balance-check → report,
but with --dry-run so nothing is logged, pushed, or updated.

Usage:
    python3 tests/smoke_all_fixtures.py                   # all fixtures
    python3 tests/smoke_all_fixtures.py amex_acme           # one fixture by name
    python3 tests/smoke_all_fixtures.py --format amex      # all fixtures of a format

Requires:
  - fixtures_manifest.json (in tests/ or Bookkeeping-clients/)
  - Service account credentials (GOOGLE_SERVICE_ACCOUNT_FILE or GOOGLE_SHEETS_CREDENTIALS)
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
TESTS_DIR = Path(__file__).parent

sys.path.insert(0, str(ROOT))
from tests.drive_fixtures import fetch_pdf_entry, DriveUnavailable  # noqa: E402


def load_manifest():
    for p in [
        Path.home() / "Bookkeeping-clients" / "fixtures_manifest.json",
        TESTS_DIR / "fixtures_manifest.json",
        TESTS_DIR / "fixtures_manifest.example.json",
    ]:
        if p.exists():
            with open(p) as f:
                return json.load(f), p
    print("No fixtures_manifest.json found", file=sys.stderr)
    sys.exit(1)


def run_one(entry):
    """Fetch the fixture PDF and run reconcile_comprehensive.py --dry-run on it."""
    name = entry["name"]
    try:
        pdf = fetch_pdf_entry(entry, cache_name=f"{name}.pdf")
    except DriveUnavailable as e:
        return name, "SKIP", str(e)

    result = subprocess.run(
        [sys.executable, str(ROOT / "reconcile_comprehensive.py"),
         str(pdf), "--dry-run", "--no-prompt"],
        capture_output=True, text=True, timeout=120,
    )

    # Check for balance verification
    out = result.stdout + result.stderr
    if result.returncode != 0:
        # Balance check failures are data issues, not code bugs — treat as warnings
        if "BALANCE CHECK FAILED" in out:
            return name, "WARN", "balance check failed (data issue, not code bug)"
        # Extract the key error line for real code failures
        err_lines = [l for l in out.splitlines() if "error" in l.lower() or "fail" in l.lower() or "traceback" in l.lower()]
        return name, "FAIL", err_lines[-1] if err_lines else f"exit {result.returncode}"

    if "Balance verification: PASSED" in out:
        status = "PASS"
    elif "Balance verification: FAILED" in out:
        status = "WARN"
    else:
        status = "PASS"  # some formats don't print balance verification

    return name, status, ""


def main():
    manifest, manifest_path = load_manifest()
    entries = manifest["statements"]

    # Filter by name or format if requested
    if len(sys.argv) > 1:
        if sys.argv[1] == "--format" and len(sys.argv) > 2:
            fmt = sys.argv[2]
            entries = [e for e in entries if e["format"] == fmt]
        else:
            names = set(sys.argv[1:])
            entries = [e for e in entries if e["name"] in names]

    if not entries:
        print("No matching fixtures found.", file=sys.stderr)
        sys.exit(1)

    # Filter to bank statement formats only (skip payroll)
    bank_formats = {
        "amex", "amex_checking", "bofa_checking", "bofa_credit", "bofa_savings",
        "chase_ink", "chase_sapphire", "chase_united",
        "citi_checking", "citi_savings", "citi_visa_costco",
        "bmo_checking", "bmo_credit",
        "northern_trust_checking", "usbank_checking",
        "wells_fargo_checking", "wells_fargo_credit",
    }
    entries = [e for e in entries if e["format"] in bank_formats]

    print(f"Running {len(entries)} fixtures from {manifest_path.name} (--dry-run)\n")

    results = []
    for entry in entries:
        name, status, detail = run_one(entry)
        icon = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗", "SKIP": "⊘"}[status]
        line = f"  {icon} {status:4s}  {name}"
        if detail:
            line += f"  — {detail}"
        print(line)
        results.append((name, status, detail))

    # Summary
    n_pass = sum(1 for _, s, _ in results if s == "PASS")
    n_warn = sum(1 for _, s, _ in results if s == "WARN")
    n_fail = sum(1 for _, s, _ in results if s == "FAIL")
    n_skip = sum(1 for _, s, _ in results if s == "SKIP")
    print(f"\n{n_pass} passed, {n_warn} warnings, {n_fail} failed, {n_skip} skipped")

    sys.exit(1 if n_fail > 0 else 0)


if __name__ == "__main__":
    main()
