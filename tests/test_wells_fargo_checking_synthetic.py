"""
test_wells_fargo_checking_synthetic.py
-----------------------------------------
Synthetic regression coverage for WellsFargoCheckingParser, purpose-built
as a companion to tests/dump_report.py for the Extract/Classify/Report
pipeline refactor (see REFACTORING_ROADMAP.md's "Architecture Proposal").

The one real fixture (confirmed via tests/dump_report.py) exercises
credits, generic debits, checks, and payroll well, but has NO bank-fees or
credit-card-payment output at all. This file covers what the real fixture
can't, plus this parser's two genuinely new-to-this-rollout mechanics:

  - Column positions that shift *within* one statement (not just between
    statements) — two different "Deposits/...Withdrawals/..." header lines
    at different character positions, with transactions on both sides
    classified using whichever columns were most recently seen.
  - A transaction whose date line has no amount at all, resolved by a
    later continuation line supplying it.
  - A check carrying both a payee AND a check number (unlike Citi
    Checking's checks, which have no payee at all).
  - A WIRE TRANS SVC CHARGE / OVERDRAFT FEE line landing in bank_fees, not
    generic debits.

The exact synthetic text below was constructed and verified against the
real (pre-migration) parser implementation directly — column-position
classification depends on precise character offsets, not just regex
content, so this isn't hand-derived from reading the regexes alone.

No real client data — client_name is left unset (None).
"""
import unittest
from decimal import Decimal


def _make_header(dep_pos, wc_pos, label="Check"):
    line = label.ljust(dep_pos) + "Deposits/"
    line = line.ljust(wc_pos) + "Withdrawals/" + " " * 6 + "Ending daily"
    return line


def _make_txn_line(date, desc, amount, target_col):
    prefix = f"     {date}  {desc}"
    return prefix.ljust(target_col) + amount


_H1 = _make_header(60, 80)    # section 1: dep_col=60, deb_col=80
_H2 = _make_header(95, 115)   # section 2: dep_col=95, deb_col=115 -- shifted

_CREDIT1 = _make_txn_line("1/05", "Contoso Deposit", "500.00", 60)
_DEBIT1  = _make_txn_line("1/10", "Contoso Debit Vendor", "80.00", 80)
_CREDIT2 = _make_txn_line("1/20", "Contoso Second Deposit", "300.00", 95)
_DEBIT2  = _make_txn_line("1/25", "Contoso Second Debit", "60.00", 115)
_BANKFEE = _make_txn_line("1/26", "Wire Trans Svc Charge", "15.00", 115)
_NOCASH_DATE = "     1/27  Contoso Continuation Vendor"
_NOCASH_CONT = " " * 110 + "35.00"  # past the section-2 midpoint -> debit
_CHECK_LINE = "     1/28 123456 Jane Vendor LLC".ljust(115) + "45.00"

_TEXT = (
    "Beginning balance on 1/1 $1,000.00\n"
    "Ending balance on 1/31 $1,565.00\n"
    "January 31, 2026 Page 1 of 3\n"
    "Transaction History\n"
    + _H1 + "\n" + _CREDIT1 + "\n" + _DEBIT1 + "\n"
    + _H2 + "\n" + _CREDIT2 + "\n" + _DEBIT2 + "\n" + _BANKFEE + "\n"
    + _NOCASH_DATE + "\n" + _NOCASH_CONT + "\n"
    + _CHECK_LINE + "\n"
    "Totals\n"
)


def _d(x):
    return Decimal(str(x))


class WellsFargoCheckingSyntheticPipelineTest(unittest.TestCase):
    def _parser(self):
        from parsers.wells_fargo import WellsFargoCheckingParser
        p = WellsFargoCheckingParser.__new__(WellsFargoCheckingParser)
        p.client_name = None
        p.beginning_balance = None
        p.ending_balance = None
        p.statement_period = None
        p.closing_date = None
        p.credits = []
        p.debits = []
        p.checks = []
        p.bank_fees = []
        p.credit_card_payments = []
        p.text = _TEXT
        return p

    def test_balances_and_period_extracted(self):
        p = self._parser()
        p.parse()
        self.assertEqual(p.beginning_balance, _d('1000.00'))
        self.assertEqual(p.ending_balance, _d('1565.00'))
        self.assertEqual(p.closing_date, '01/31/26')

    def test_first_section_credit_and_debit_classified(self):
        p = self._parser()
        p.parse()
        credit_vendors = {c['vendor']: c for c in p.credits}
        debit_vendors = {d['vendor']: d for d in p.debits}
        self.assertIn('Contoso Deposit', credit_vendors)
        self.assertEqual(credit_vendors['Contoso Deposit']['amount'], _d('500.00'))
        self.assertIn('Contoso Debit Vendor', debit_vendors)
        self.assertEqual(debit_vendors['Contoso Debit Vendor']['amount'], _d('80.00'))

    def test_column_shift_within_statement_reclassifies_correctly(self):
        # Section 2's header moves dep_col/deb_col further right. If the
        # migration accidentally kept using section 1's columns, these two
        # transactions would misclassify (their positions fall in section
        # 1's "past both columns" zone, not section 1's credit/debit split).
        p = self._parser()
        p.parse()
        credit_vendors = {c['vendor']: c for c in p.credits}
        debit_vendors = {d['vendor']: d for d in p.debits}
        self.assertIn('Contoso Second Deposit', credit_vendors)
        self.assertEqual(credit_vendors['Contoso Second Deposit']['amount'], _d('300.00'))
        self.assertIn('Contoso Second Debit', debit_vendors)
        self.assertEqual(debit_vendors['Contoso Second Debit']['amount'], _d('60.00'))

    def test_bank_fee_lands_in_bank_fees_not_debits(self):
        p = self._parser()
        p.parse()
        fee_vendors = {f['vendor'] for f in p.bank_fees}
        self.assertIn('Wire Trans Svc Charge', fee_vendors)
        self.assertNotIn('Wire Trans Svc Charge', {d['vendor'] for d in p.debits})
        self.assertEqual(len(p.bank_fees), 1)
        self.assertEqual(p.bank_fees[0]['amount'], _d('15.00'))

    def test_continuation_line_supplies_missing_amount(self):
        p = self._parser()
        p.parse()
        debit_vendors = {d['vendor']: d for d in p.debits}
        self.assertIn('Contoso Continuation Vendor', debit_vendors)
        self.assertEqual(debit_vendors['Contoso Continuation Vendor']['amount'], _d('35.00'))

    def test_check_carries_both_payee_and_check_number(self):
        p = self._parser()
        p.parse()
        self.assertEqual(len(p.checks), 1)
        chk = p.checks[0]
        self.assertEqual(chk['check_num'], '123456')
        self.assertEqual(chk['payee'], 'Jane Vendor LLC')
        self.assertEqual(chk['amount'], _d('45.00'))

    def test_bucket_counts_exhaustive(self):
        p = self._parser()
        p.parse()
        self.assertEqual(len(p.credits), 2)
        self.assertEqual(len(p.debits), 3)
        self.assertEqual(len(p.checks), 1)
        self.assertEqual(len(p.bank_fees), 1)

    def test_report_balances(self):
        p = self._parser()
        p.parse()
        report = p.generate_report()
        self.assertIn('Balance verification: PASSED', report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
