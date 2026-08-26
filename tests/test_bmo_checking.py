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


def _base_manual_data():
    """Two deposits, one check -- deliberately balances beginning/ending too,
    so the existing balance check can't mask a count/amount cross-check bug."""
    return {
        'beginning_balance': '1000.00',
        'ending_balance': '1350.00',
        'credits': [
            {'date': '07/01/26', 'vendor': 'Acme Corp Deposit', 'amount': '300.00'},
            {'date': '07/02/26', 'vendor': 'Bravo LLC Deposit', 'amount': '200.00'},
        ],
        'checks': [
            {'date': '07/03/26', 'number': '101', 'amount': '150.00', 'vendor': 'Charlie Vendor'},
        ],
        'debits': [],
    }


def test_generate_report_stated_totals_match_no_warning():
    data = _base_manual_data()
    data.update(stated_deposit_count=2, stated_deposit_amount='500.00',
                 stated_withdrawal_count=1, stated_withdrawal_amount='150.00')
    p = BMOCheckingParser(pdf_path=None, client_name='Acme Corp')
    p.load_from_dict(data)
    report = p.generate_report()
    assert 'Deposit/withdrawal count and totals match' in report
    assert "doesn't match" not in report
    print("PASS  test_generate_report_stated_totals_match_no_warning")


def test_generate_report_no_stated_totals_no_crosscheck_section():
    p = BMOCheckingParser(pdf_path=None, client_name='Acme Corp')
    p.load_from_dict(_base_manual_data())
    report = p.generate_report()
    assert 'Account Summary' not in report
    print("PASS  test_generate_report_no_stated_totals_no_crosscheck_section")


def test_generate_report_deposit_count_matches_amount_does_not():
    """Real bug shape: count right, total wrong -- means one itemized
    deposit amount is misread, not a missing transaction."""
    data = _base_manual_data()
    data.update(stated_deposit_count=2, stated_deposit_amount='550.00',
                 stated_withdrawal_count=1, stated_withdrawal_amount='150.00')
    p = BMOCheckingParser(pdf_path=None, client_name='Acme Corp')
    p.load_from_dict(data)
    report = p.generate_report()
    assert "doesn't match" in report
    assert 'Deposit total: statement says $550.00, itemized $500.00' in report
    assert 'Deposit count' not in report  # count itself matched -- only the total line should fire
    print("PASS  test_generate_report_deposit_count_matches_amount_does_not")


def test_generate_report_withdrawal_amount_matches_count_does_not():
    """Real bug shape: total right, count wrong -- means two real
    transactions got collapsed into one itemized line."""
    data = _base_manual_data()
    data.update(stated_deposit_count=2, stated_deposit_amount='500.00',
                 stated_withdrawal_count=2, stated_withdrawal_amount='150.00')
    p = BMOCheckingParser(pdf_path=None, client_name='Acme Corp')
    p.load_from_dict(data)
    report = p.generate_report()
    assert 'Withdrawal count: statement says 2, itemized 1' in report
    assert 'Withdrawal total' not in report  # amount itself matched -- only the count line should fire
    print("PASS  test_generate_report_withdrawal_amount_matches_count_does_not")


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

Account Summary
BEGINNING BALANCE AS   NUMBER OF   DEPOSIT      NUMBER OF     WITHDRAWAL    SERVICE   ENDING BALANCE AS OF
OF JUNE 30, 2026        DEPOSITS    AMOUNT       WITHDRAWALS   AMOUNT        CHARGES   JULY 31, 2026
$27,897.91              56          $99,281.97   50            $94,615.45    $0.00     $32,564.43

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


def test_parse_stated_deposit_withdrawal_totals():
    """The Account Summary row (beginning balance, deposit count/amount,
    withdrawal count/amount, service charges, ending balance) is one row
    in the real statement -- parse() should pull the count/amount fields
    from it regardless of the label row above wrapping onto two lines."""
    p = BMOCheckingParser(pdf_path=None)
    p.text = _SYNTHETIC_TEXT
    p.parse()
    assert p.stated_deposit_count == 56, p.stated_deposit_count
    assert p.stated_deposit_amount == Decimal('99281.97'), p.stated_deposit_amount
    assert p.stated_withdrawal_count == 50, p.stated_withdrawal_count
    assert p.stated_withdrawal_amount == Decimal('94615.45'), p.stated_withdrawal_amount
    print("PASS  test_parse_stated_deposit_withdrawal_totals")


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
    test_generate_report_stated_totals_match_no_warning,
    test_generate_report_no_stated_totals_no_crosscheck_section,
    test_generate_report_deposit_count_matches_amount_does_not,
    test_generate_report_withdrawal_amount_matches_count_does_not,
    test_load_from_dict_closing_date_passthrough,
    test_parse_closing_date_from_through_clause,
    test_parse_statement_period_from_through_clause,
    test_parse_stated_deposit_withdrawal_totals,
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
