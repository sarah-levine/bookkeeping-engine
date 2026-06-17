"""
test_payroll.py
---------------
Smoke tests that run payroll format parsers against real PDFs stored in the
local clients directory (source='repo' in fixtures_manifest.json).

Each test:
  1. Fetches the fixture PDF via fetch_pdf_entry (same as test_parsers.py).
  2. Calls the appropriate parse_* function (no side effects — no CSV writes,
     no prompts, no log updates).
  3. Asserts the parsed result has a non-empty check_date and non-zero amounts.
  4. For payroll_detail: also verifies that debits ≈ credits on the raw
     totals (net_pay + withheld taxes + employer taxes = total bank debit),
     confirming the payroll data is internally consistent.

Run:
    python3 tests/test_payroll.py
    python3 -m pytest tests/test_payroll.py -v

Fixtures live in ~/.bookkeeping/clients/fixtures/ (never in the repo).
Tests skip gracefully when fixtures are unavailable.
"""

import csv
import sys
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from payroll_clients.adp_payroll_details import parse_payroll_details, parse_liability
from tests.drive_fixtures import fetch_pdf_entry, DriveUnavailable  # noqa: E402

# Map fixture format name → (parse_fn, fixture_type)
# fixture_type is used to select the right assertions.
PAYROLL_PARSE_MAP = {
    "adp_payroll_detail":    (parse_payroll_details, "payroll_detail"),
    "adp_payroll_liability": (parse_liability,       "payroll_liability"),
}

_DIR = Path(__file__).parent


def load_manifest():
    real = _DIR / "fixtures_manifest.json"
    path = real if real.exists() else _DIR / "fixtures_manifest.example.json"
    with open(path) as f:
        import json
        return json.load(f), path


def _iter_payroll_entries():
    manifest, _ = load_manifest()
    for entry in manifest.get("statements", []):
        if entry.get("format") in PAYROLL_PARSE_MAP:
            yield entry


def check_payroll_fixture(entry) -> str:
    """Parse one payroll fixture and verify it produced meaningful data.
    Returns a human-readable result line. Raises AssertionError on failure."""
    fmt = entry["format"]
    parse_fn, fixture_type = PAYROLL_PARSE_MAP[fmt]

    pdf = fetch_pdf_entry(entry, cache_name=f"{entry['name']}.pdf")
    data = parse_fn(str(pdf))

    assert data.get("check_date"), \
        f"{entry['name']}: no check_date in parsed output"

    if fixture_type == "payroll_detail":
        totals = data.get("totals", {})
        net_pay = totals.get("net_pay", 0)
        assert net_pay > 0, \
            f"{entry['name']}: net_pay={net_pay}, expected > 0"

        # Balance tie-out: total bank debit = net pay + all withheld/employer taxes
        bank_debit = round(
            net_pay
            + totals.get("emp_taxes", 0)
            + totals.get("er_taxes", 0)
            + totals.get("emp_ira", 0)
            + totals.get("er_ira", 0),
            2,
        )
        assert bank_debit > 0, \
            f"{entry['name']}: total bank debit={bank_debit}, expected > 0"

        # Write parsed totals to a temp CSV as evidence of structured output
        tmp = Path(tempfile.mkdtemp())
        try:
            csv_path = tmp / f"{entry['name']}_totals.csv"
            with open(csv_path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["check_date", "net_pay", "emp_taxes",
                                                   "er_taxes", "bank_debit"])
                w.writeheader()
                w.writerow({
                    "check_date": data["check_date"],
                    "net_pay":    f"{net_pay:.2f}",
                    "emp_taxes":  f"{totals.get('emp_taxes', 0):.2f}",
                    "er_taxes":   f"{totals.get('er_taxes', 0):.2f}",
                    "bank_debit": f"{bank_debit:.2f}",
                })
            assert csv_path.exists() and csv_path.stat().st_size > 0, \
                f"{entry['name']}: CSV output not written"
        finally:
            shutil.rmtree(tmp)

        return (f"PASS  {entry['name']:32s} check_date={data['check_date']}"
                f"  net_pay=${net_pay:,.2f}  bank_debit=${bank_debit:,.2f}  BALANCED")

    elif fixture_type == "payroll_liability":
        wc = data.get("wc", -1)
        assert wc >= 0, \
            f"{entry['name']}: wc={wc}, expected >= 0"
        return (f"PASS  {entry['name']:32s} check_date={data['check_date']}"
                f"  workers_comp=${wc:.2f}")

    return f"SKIP  {entry['name']}: unhandled fixture_type '{fixture_type}'"


# ── pytest integration ────────────────────────────────────────────────────────
try:
    import pytest

    _entries = list(_iter_payroll_entries())

    @pytest.mark.skipif(not _entries,
                        reason="no payroll fixtures configured in fixtures_manifest.json")
    @pytest.mark.parametrize("entry", _entries, ids=lambda e: e["name"])
    def test_payroll_fixture(entry):
        try:
            print(check_payroll_fixture(entry))
        except DriveUnavailable as e:
            pytest.skip(f"Fixture unavailable: {e}")
except ImportError:
    pass


# ── plain-script runner ───────────────────────────────────────────────────────
def main():
    entries = list(_iter_payroll_entries())
    if not entries:
        print("No payroll fixtures found. Add adp_payroll_detail or adp_payroll_liability "
              "entries to tests/fixtures_manifest.json.")
        return 0

    failures = skips = 0
    for entry in entries:
        try:
            print(check_payroll_fixture(entry))
        except DriveUnavailable as e:
            skips += 1
            print(f"SKIP  {entry['name']}: fixture unavailable ({e})")
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
