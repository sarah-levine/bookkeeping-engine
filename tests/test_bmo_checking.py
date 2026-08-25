"""
test_bmo_checking.py
---------------------
Unit tests for BMOCheckingParser's closing_date/statement_period extraction
(load_from_dict and parse()). No PDFs, no Drive, no network -- runs anywhere.

Companion to test_bmo_credit.py's cardholder-name coverage; this file exists
because BMOCheckingParser never set self.closing_date/self.statement_date at
all (see REFACTORING_ROADMAP.md), so every real reconciliation through this
parser logged a blank statement date.

Run:
    python3 tests/test_bmo_checking.py
"""

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from parsers.bmo import BMOCheckingParser  # noqa: E402


# ── load_from_dict ────────────────────────────────────────────────────────────

def test_load_from_dict_no_closing_date_defaults_none():
    p = BMOCheckingParser(pdf_path=None, client_name='Acme Corp')
    p.load_from_dict({'beginning_balance': '100.00', 'ending_balance': '100.00'})
    assert p.closing_date is None
    assert p.statement_date is None
    print("PASS  test_load_from_dict_no_closing_date_defaults_none")


def test_load_from_dict_closing_date_passthrough():
    p = BMOCheckingParser(pdf_path=None, client_name='Acme Corp')
    p.load_from_dict({
        'beginning_balance': '100.00', 'ending_balance': '100.00',
        'closing_date': '07/31/26', 'statement_period': 'July 31, 2026',
    })
    assert p.closing_date == '07/31/26'
    assert p.statement_date == '07/31/26'
    assert p.statement_period == 'July 31, 2026'
    print("PASS  test_load_from_dict_closing_date_passthrough")


# ── parse() from synthetic pdftotext text ─────────────────────────────────────
# Wording ("<Month> <Day>, <Year> through <Month> <Day>, <Year>") matches the
# real statement's own header layout, confirmed against a real photographed
# BMO Premium Business Checking statement -- not a hypothetical guess at the
# format. No real PDF fixture exists yet to verify the pdftotext -layout
# whitespace/column behavior against (see REFACTORING_ROADMAP.md); upload one
# via Mode H to close that gap.

_SYNTHETIC_TEXT = """\
BMO Premium Business Checking

Date
July 01, 2026 through
July 31, 2026

Statement Summary

BEGINNING BALANCE AS OF JUNE 30, 2026    $27,897.91
ENDING BALANCE AS OF JULY 31, 2026       $32,564.43

Monthly Activity Details
Date        Transaction description              Withdrawal      Deposit      Balance
Jul 01      ACH DEPOSIT                                           $1,124.55
            CCD iWallet cards  iWallet ca
"""


def test_parse_closing_date_from_through_clause():
    p = BMOCheckingParser(pdf_path=None)
    p.text = _SYNTHETIC_TEXT
    p.parse()
    assert p.closing_date == '07/31/26', p.closing_date
    assert p.statement_date == '07/31/26', p.statement_date
    print("PASS  test_parse_closing_date_from_through_clause")


def test_parse_statement_period_from_through_clause():
    p = BMOCheckingParser(pdf_path=None)
    p.text = _SYNTHETIC_TEXT
    p.parse()
    assert p.statement_period == 'July 31, 2026', p.statement_period
    print("PASS  test_parse_statement_period_from_through_clause")


def test_parse_no_through_clause_leaves_closing_date_none():
    """A statement with no 'through' clause shouldn't crash -- closing_date
    stays None rather than raising, same fallback shape as the credit card
    parser when it can't find a date."""
    p = BMOCheckingParser(pdf_path=None)
    p.text = "BEGINNING BALANCE  $100.00\nENDING BALANCE  $100.00\n"
    p.parse()
    assert p.closing_date is None
    print("PASS  test_parse_no_through_clause_leaves_closing_date_none")


def test_parse_still_extracts_balances_and_transactions():
    """Closing-date extraction must not regress the existing balance/
    transaction parsing this class already had."""
    p = BMOCheckingParser(pdf_path=None)
    p.text = _SYNTHETIC_TEXT
    p.parse()
    assert p.beginning_balance == Decimal('27897.91')
    assert p.ending_balance == Decimal('32564.43')
    assert len(p.credits) == 1
    assert p.credits[0]['amount'] == Decimal('1124.55')
    print("PASS  test_parse_still_extracts_balances_and_transactions")


# ── runner ────────────────────────────────────────────────────────────────────

TESTS = [
    test_load_from_dict_no_closing_date_defaults_none,
    test_load_from_dict_closing_date_passthrough,
    test_parse_closing_date_from_through_clause,
    test_parse_statement_period_from_through_clause,
    test_parse_no_through_clause_leaves_closing_date_none,
    test_parse_still_extracts_balances_and_transactions,
]


def main():
    failures = 0
    for t in TESTS:
        try:
            t()
        except Exception as e:
            import traceback
            failures += 1
            print(f"FAIL  {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{'All tests passed.' if not failures else f'{failures} failure(s).'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
