"""
test_bmo_credit.py
------------------
Unit tests for BMOCreditCardParser: load_from_dict, _expand_date,
generate_report (balance check, charges, payments, credits), parse()
from synthetic pdftotext text, and vendor normalization.

No PDFs, no Drive, no network — runs anywhere.

Run:
    python3 tests/test_bmo_credit.py
"""

import sys
import tempfile
import json
import shutil
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from parsers.bmo import BMOCreditCardParser  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_parser(**kwargs):
    """Return a BMOCreditCardParser populated via load_from_dict()."""
    p = BMOCreditCardParser(client_name=kwargs.pop('client_name', 'Acme Corp'))
    p.load_from_dict(kwargs)
    return p


def _base_data():
    return {
        'previous_balance': '109.99',
        'new_balance':      '821.45',
        'total_payments':   '109.99',
        'statement_period': 'May 24, 2026',
        'payments': [
            {'date': '04/26/26', 'description': 'PAYMENT - THANK YOU', 'amount': Decimal('109.99')},
        ],
        'credits': [],
        'charges': [
            {'date': '04/27/26', 'vendor': 'ACME SUPPLIES INC',     'amount': Decimal('229.32')},
            {'date': '04/27/26', 'vendor': 'BRAVO PARTS LLC 1',      'amount': Decimal('173.54')},
            {'date': '04/27/26', 'vendor': 'BRAVO PARTS LLC 2',      'amount': Decimal('245.82')},
            {'date': '04/27/26', 'vendor': 'BRAVO PARTS LLC 3',      'amount': Decimal('103.09')},
            {'date': '04/29/26', 'vendor': 'CHARLIE GOV FEE 1',     'amount': Decimal('28.00')},
            {'date': '04/29/26', 'vendor': 'CHARLIE GOV FEE 2',     'amount': Decimal('0.55')},
            {'date': '05/19/26', 'vendor': 'APPLE.COM/BILL',        'amount': Decimal('0.99')},
            {'date': '05/20/26', 'vendor': 'AMAZON MKTPL',          'amount': Decimal('40.14')},
        ],
    }


# ── date normalization ────────────────────────────────────────────────────────

def test_expand_date_already_four_digit():
    p = BMOCreditCardParser()
    p.statement_period = 'May 24, 2026'
    assert p._expand_date('04/27/2026') == '04/27/2026'
    print("PASS  test_expand_date_already_four_digit")


def test_expand_date_two_digit_year():
    p = BMOCreditCardParser()
    p.statement_period = 'May 24, 2026'
    assert p._expand_date('04/27/26') == '04/27/2026'
    print("PASS  test_expand_date_two_digit_year")


def test_expand_date_no_year():
    p = BMOCreditCardParser()
    p.statement_period = 'May 24, 2026'
    assert p._expand_date('04/27') == '04/27/2026'
    print("PASS  test_expand_date_no_year")


def test_expand_date_falls_back_to_current_year():
    """When no statement_period is set, fall back to datetime.now().year."""
    from datetime import datetime
    p = BMOCreditCardParser()
    result = p._expand_date('04/27')
    assert result == f'04/27/{datetime.now().year}'
    print("PASS  test_expand_date_falls_back_to_current_year")


def test_expand_date_century_boundary():
    """Two-digit year >= 70 maps to 19xx, < 70 maps to 20xx."""
    p = BMOCreditCardParser()
    p.statement_period = 'January 1, 2000'
    assert p._expand_date('01/01/99') == '01/01/1999'
    assert p._expand_date('01/01/00') == '01/01/2000'
    print("PASS  test_expand_date_century_boundary")


# ── load_from_dict ────────────────────────────────────────────────────────────

def test_load_from_dict_normalizes_dates():
    p = _make_parser(**_base_data())
    for c in p.charges:
        assert len(c['date'].split('/')) == 3
        assert len(c['date'].split('/')[-1]) == 4, f"Expected 4-digit year in {c['date']}"
    print("PASS  test_load_from_dict_normalizes_dates")


def test_load_from_dict_balances():
    p = _make_parser(**_base_data())
    assert p.previous_balance == Decimal('109.99')
    assert p.new_balance      == Decimal('821.45')
    assert p.total_payments   == Decimal('109.99')
    print("PASS  test_load_from_dict_balances")


def test_load_from_dict_statement_period():
    p = _make_parser(**_base_data())
    assert p.statement_period == 'May 24, 2026'
    print("PASS  test_load_from_dict_statement_period")


def test_load_from_dict_transaction_counts():
    p = _make_parser(**_base_data())
    assert len(p.charges)  == 8
    assert len(p.payments) == 1
    assert len(p.credits)  == 0
    print("PASS  test_load_from_dict_transaction_counts")


# ── generate_report ───────────────────────────────────────────────────────────

def test_generate_report_balance_check_passes():
    p = _make_parser(**_base_data())
    report = p.generate_report()
    assert '✓ Balance verification: PASSED' in report
    print("PASS  test_generate_report_balance_check_passes")


def test_generate_report_balance_check_fails():
    data = _base_data()
    data['new_balance'] = '999.99'  # wrong
    p = _make_parser(**data)
    report = p.generate_report()
    assert '✗ Balance verification: FAILED' in report
    print("PASS  test_generate_report_balance_check_fails")


def test_generate_report_charges_total():
    p = _make_parser(**_base_data())
    report = p.generate_report()
    assert 'TOTAL CHARGES:' in report
    assert '$         821.45' in report
    print("PASS  test_generate_report_charges_total")


def test_generate_report_payments_section():
    p = _make_parser(**_base_data())
    report = p.generate_report()
    assert 'PAYMENTS' in report
    assert 'PAYMENT - THANK YOU' in report
    assert 'TOTAL PAYMENTS:' in report
    print("PASS  test_generate_report_payments_section")


def test_generate_report_dates_four_digit():
    p = _make_parser(**_base_data())
    report = p.generate_report()
    assert '04/27/2026' in report
    assert '04/27/26' not in report
    print("PASS  test_generate_report_dates_four_digit")


def test_generate_report_header():
    p = _make_parser(**_base_data())
    report = p.generate_report()
    assert 'BMO BUSINESS PLATINUM REWARDS CREDIT CARD' in report
    assert 'Acme Corp' in report
    assert 'May 24, 2026' in report
    print("PASS  test_generate_report_header")


def test_generate_report_credits_section():
    data = _base_data()
    data['credits'] = [
        {'date': '05/10/26', 'description': 'RETURN CREDIT', 'amount': Decimal('25.00')},
    ]
    data['charges'].append({'date': '05/10/26', 'vendor': 'SOME STORE', 'amount': Decimal('25.00')})
    p = _make_parser(**data)
    report = p.generate_report()
    assert 'CREDITS' in report
    assert 'RETURN CREDIT' in report
    print("PASS  test_generate_report_credits_section")


# ── vendor normalization (requires a client config) ───────────────────────────

def test_normalize_vendor_no_config():
    """Without a client config, normalize_vendor returns the vendor as-is."""
    p = BMOCreditCardParser(client_name='Unknown Client XYZ')
    result = p.normalize_vendor('DELTA STORE INC')
    assert result == 'DELTA STORE INC'
    print("PASS  test_normalize_vendor_no_config")


def test_normalize_vendor_with_config():
    """With a client config that has vendor_rules, normalization applies."""
    d = Path(tempfile.mkdtemp())
    try:
        cfg = {
            "client_name": "Fabrikam LLC",
            "canonical_name": "FABRIKAM LLC",
            "statement_types": ["bmo_credit"],
            "vendor_rules": [
                {"contains": "ACME SUPPLY", "normalize_to": "Acme Supply Co"},
                {"contains": "AMAZON MKTPL", "normalize_to": "Amazon"},
            ]
        }
        (d / "fabrikam.json").write_text(json.dumps(cfg))
        from parsers.base import ClientRegistry
        import parsers.bmo as _bmo_mod
        old_registry = _bmo_mod._registry
        _bmo_mod._registry = ClientRegistry(clients_dir=str(d))
        try:
            p = BMOCreditCardParser(client_name='FABRIKAM LLC')
            assert p.normalize_vendor('ACME SUPPLY 12345') == 'Acme Supply Co'
            assert p.normalize_vendor('AMAZON MKTPL ORDER') == 'Amazon'
            assert p.normalize_vendor('DELTA STORE INC') == 'DELTA STORE INC'
        finally:
            _bmo_mod._registry = old_registry
        print("PASS  test_normalize_vendor_with_config")
    finally:
        shutil.rmtree(d)


# ── parse() from synthetic pdftotext text ─────────────────────────────────────

_SYNTHETIC_TEXT = """\
BMO Business Platinum Rewards Credit Card
Account Number ending in 0971

STATEMENT CLOSE DATE  May 24, 2026

PREVIOUS BALANCE   $109.99
NEW BALANCE        $821.45

TRANSACTION DETAILS
Date        Date       Description                          Amount
04/24       04/27      ACME SUPPLIES REF001                229.32
04/24       04/27      BRAVO PARTS LLC REF002               100.00 CR
05/18       05/19      PAYMENT - THANK YOU                 109.99
"""


def test_parse_previous_and_new_balance():
    p = BMOCreditCardParser()
    p.text = _SYNTHETIC_TEXT
    p.parse()
    assert p.previous_balance == Decimal('109.99'), p.previous_balance
    assert p.new_balance      == Decimal('821.45'), p.new_balance
    print("PASS  test_parse_previous_and_new_balance")


def test_parse_statement_period():
    p = BMOCreditCardParser()
    p.text = _SYNTHETIC_TEXT
    p.parse()
    assert p.statement_period == 'May 24, 2026', p.statement_period
    print("PASS  test_parse_statement_period")


def test_parse_transactions():
    p = BMOCreditCardParser()
    p.text = _SYNTHETIC_TEXT
    p.parse()
    assert len(p.charges)  == 1, f"expected 1 charge, got {len(p.charges)}"
    assert len(p.credits)  == 1, f"expected 1 credit, got {len(p.credits)}"
    assert len(p.payments) == 1, f"expected 1 payment, got {len(p.payments)}"
    print("PASS  test_parse_transactions")


def test_parse_dates_are_four_digit():
    p = BMOCreditCardParser()
    p.text = _SYNTHETIC_TEXT
    p.parse()
    for lst in (p.charges, p.credits, p.payments):
        for t in lst:
            parts = t['date'].split('/')
            assert len(parts) == 3 and len(parts[2]) == 4, \
                f"Expected MM/DD/YYYY, got {t['date']!r}"
    print("PASS  test_parse_dates_are_four_digit")


def test_parse_empty_text():
    """parse() on empty text should not raise and leave lists empty."""
    p = BMOCreditCardParser()
    p.text = ''
    p.parse()
    assert p.charges == [] and p.payments == [] and p.credits == []
    print("PASS  test_parse_empty_text")


# ── statement_type attribute ──────────────────────────────────────────────────

def test_statement_type_attribute():
    assert BMOCreditCardParser.statement_type == "BMO Business Platinum Rewards Credit Card"
    print("PASS  test_statement_type_attribute")


# ── pytest integration ────────────────────────────────────────────────────────
try:
    import pytest  # noqa: F401
except ImportError:
    pass


# ── runner ────────────────────────────────────────────────────────────────────

TESTS = [
    test_expand_date_already_four_digit,
    test_expand_date_two_digit_year,
    test_expand_date_no_year,
    test_expand_date_falls_back_to_current_year,
    test_expand_date_century_boundary,
    test_load_from_dict_normalizes_dates,
    test_load_from_dict_balances,
    test_load_from_dict_statement_period,
    test_load_from_dict_transaction_counts,
    test_generate_report_balance_check_passes,
    test_generate_report_balance_check_fails,
    test_generate_report_charges_total,
    test_generate_report_payments_section,
    test_generate_report_dates_four_digit,
    test_generate_report_header,
    test_generate_report_credits_section,
    test_normalize_vendor_no_config,
    test_normalize_vendor_with_config,
    test_parse_previous_and_new_balance,
    test_parse_statement_period,
    test_parse_transactions,
    test_parse_dates_are_four_digit,
    test_parse_empty_text,
    test_statement_type_attribute,
]


def main():
    failures = skips = 0
    for t in TESTS:
        try:
            t()
        except unittest.SkipTest as e:
            skips += 1
            print(f"SKIP  {t.__name__}: {e}")
        except Exception as e:
            import traceback
            failures += 1
            print(f"FAIL  {t.__name__}: {e}")
            traceback.print_exc()
    summary = "All tests passed." if not failures else f"{failures} failure(s)."
    if skips:
        summary += f" {skips} skipped."
    print(f"\n{summary}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
