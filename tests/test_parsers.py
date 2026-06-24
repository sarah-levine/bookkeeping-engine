"""
test_parsers.py
---------------
Smoke tests that run each statement parser against a real PDF pulled from
Google Drive (see drive_fixtures.py). The real PDFs never live in the repo —
only Drive file IDs in fixtures_manifest.json (gitignored).

Run:
    GOOGLE_SHEETS_CREDENTIALS="$(cat ~/Downloads/<service-account>.json)" \
        python3 -m pytest tests/test_parsers.py -v

    # or without pytest:
    GOOGLE_SHEETS_CREDENTIALS="..." python3 tests/test_parsers.py

If no manifest or no Drive credentials are present, the tests skip rather
than fail — so a public checkout with no secrets stays green.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from parsers import (  # noqa: E402
    ChaseInkParser, ChaseUnitedParser, ChaseSapphireParser,
    AmexStatementParser, AmexCheckingParser,
    BankOfAmericaCreditCardParser, BankOfAmericaCheckingParser, BankOfAmericaSavingsParser,
    CitiCheckingParser, CitiVisaCostcoParser, CitiSavingsParser,
    BMOCheckingParser, BMOCreditCardParser, NorthernTrustCheckingParser, USBankCheckingParser,
    WellsFargoCreditCardParser, WellsFargoCheckingParser,
)
from tests.drive_fixtures import fetch_pdf_entry, drive_available, DriveUnavailable  # noqa: E402

PARSER_MAP = {
    "chase_ink":               ChaseInkParser,
    "chase_sapphire":          ChaseSapphireParser,
    "chase_united":            ChaseUnitedParser,
    "amex":                    AmexStatementParser,
    "amex_checking":           AmexCheckingParser,
    "bofa_credit":             BankOfAmericaCreditCardParser,
    "bofa_checking":           BankOfAmericaCheckingParser,
    "bofa_savings":            BankOfAmericaSavingsParser,
    "citi_checking":           CitiCheckingParser,
    "citi_savings":            CitiSavingsParser,
    "citi_visa_costco":        CitiVisaCostcoParser,
    "bmo_checking":            BMOCheckingParser,
    "bmo_credit":              BMOCreditCardParser,
    "northern_trust_checking": NorthernTrustCheckingParser,
    "usbank_checking":         USBankCheckingParser,
    "wells_fargo_credit":      WellsFargoCreditCardParser,
    "wells_fargo_checking":    WellsFargoCheckingParser,
}

_DIR = Path(__file__).parent


def load_manifest():
    """Prefer the real (gitignored) manifest; fall back to the example."""
    real = _DIR / "fixtures_manifest.json"
    path = real if real.exists() else _DIR / "fixtures_manifest.example.json"
    with open(path) as f:
        return json.load(f), path


def check_fixture(entry) -> str:
    """Download + parse one fixture. Returns a human-readable result line.
    Raises AssertionError on a real failure."""
    fmt = entry["format"]
    parser_cls = PARSER_MAP.get(fmt)
    if not parser_cls:
        return f"SKIP  {entry['name']}: unknown format '{fmt}'"

    pdf = fetch_pdf_entry(entry, cache_name=f"{entry['name']}.pdf")
    parser = parser_cls(str(pdf))
    parser.parse()

    # Mirror reconcile_comprehensive.py's own success check: a parse worked if
    # it pulled at least one balance off the statement.
    has_balances = any(
        getattr(parser, attr, None)
        for attr in ("previous_balance", "new_balance", "beginning_balance", "ending_balance")
    )
    # Count line items from whichever transaction lists the parser populates.
    n = sum(
        len(getattr(parser, attr, []) or [])
        for attr in ("charges", "credits", "payments", "deposits", "withdrawals")
    )
    if not (has_balances or n > 0):
        # OCR-only parsers store None in _ocr_text when tesseract isn't installed.
        if hasattr(parser, '_ocr_text') and parser._ocr_text is None:
            raise DriveUnavailable(f"{entry['name']}: OCR unavailable (tesseract not installed?)")
        assert False, f"{entry['name']}: parser produced no balances and no line items"

    expect = entry.get("expect_client")
    if expect:
        assert parser.client_name == expect, \
            f"{entry['name']}: expected client '{expect}', got '{parser.client_name}'"

    return f"PASS  {entry['name']:24s} client={parser.client_name!r} items={n} balances={has_balances}"


def _iter_real_entries():
    manifest, path = load_manifest()
    using_example = path.name.endswith(".example.json")
    for entry in manifest.get("statements", []):
        # Skip unconfigured Drive placeholders; local-source entries have no file_id
        if entry.get("source", "drive") == "drive" and entry.get("file_id", "REPLACE_ME") == "REPLACE_ME":
            continue
        yield entry, using_example


# ── pytest integration (optional) ───────────────────────────────────────────
try:
    import pytest

    _entries = [e for e, _ in _iter_real_entries()]

    @pytest.mark.skipif(not _entries,
                        reason="no configured fixtures (copy fixtures_manifest.example.json → fixtures_manifest.json)")
    @pytest.mark.parametrize("entry", _entries, ids=lambda e: e["name"])
    def test_parser_fixture(entry):
        try:
            print(check_fixture(entry))
        except DriveUnavailable as e:
            pytest.skip(f"Fixture unavailable: {e}")
except ImportError:
    pass


# ── plain-script runner ─────────────────────────────────────────────────────
def main():
    entries = list(_iter_real_entries())
    if not entries:
        print("No configured fixtures. Copy fixtures_manifest.example.json → "
              "fixtures_manifest.json and fill in Drive file IDs.")
        return 0

    failures = 0
    for entry, _ in entries:
        try:
            print(check_fixture(entry))
        except DriveUnavailable as e:
            print(f"SKIP  {entry['name']}: Drive unavailable ({e})")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {e}")
        except Exception as e:  # parser blew up
            failures += 1
            print(f"ERROR {entry['name']}: {type(e).__name__}: {e}")
    print(f"\n{'All fixtures passed.' if not failures else f'{failures} failure(s).'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
