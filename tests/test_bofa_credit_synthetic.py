"""
test_bofa_credit_synthetic.py
-------------------------------
Synthetic regression coverage for BankOfAmericaCreditCardParser, purpose-
built as a companion to tests/dump_report.py for the Extract/Classify/
Report pipeline refactor (see REFACTORING_ROADMAP.md's "Architecture
Proposal").

The two real fixtures (confirmed via tests/dump_report.py) exercise
payments, charges, a negative (credit-balance) new_balance, and a genuine
finance_charge — but not: a genuine credit/return classified within the
payments section (vs. a plain payment), the finance-charge-line skip
*within* the charges section specifically, or a negative-signed charge
surviving with its sign intact (this parser's charges bucket has a
genuinely mixed sign convention, unlike every other parser migrated in
this rollout — self.payments/self.credits always store abs(amount), but
self.charges stores the raw signed value with no forcing at all).

The synthetic text below was constructed and verified against the real
(pre-migration) parser directly, since the 20+-digit reference-number
regex and section-header state tracking need exact structural matching.

No real client data — client_name is left unset (None) for the main
scenario; the bofa_credits_account scenario uses a temporary fictional
client config.
"""
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import parsers.base as _base_mod
import parsers.bofa as _bofa_mod
from parsers.base import ClientRegistry
from parsers.bofa import BankOfAmericaCreditCardParser

_REF = "12345678901234567890"  # 20+ digit reference number the regex requires


def _txn_line(post, trans, desc, amount_str):
    return f"{post} {trans} {desc}  {_REF} {amount_str}"


_TEXT = (
    "December 07, 2025 - January 06, 2026\n"
    "Statement Closing Date ....... 01/06/26\n"
    "Previous Balance ....... $500.00\n"
    "New Balance Total ....... $290.00\n"
    "Payments and Other Credits\n"
    + _txn_line("12/10", "12/09", "ONLINE PAYMENT - THANK YOU", "-200.00") + "\n"
    # Genuine credit/return within the payments section -- classified via
    # _classify_cc_transaction's 'RETURN' keyword match, not just a payment.
    + _txn_line("12/12", "12/11", "CONTOSO MERCHANDISE RETURN", "-50.00") + "\n"
    "TOTAL PAYMENTS\n"
    "Purchases and Other Charges\n"
    + _txn_line("12/15", "12/14", "CONTOSO OFFICE SUPPLIES", "45.00") + "\n"
    # Negative-signed charge -- must survive with its sign intact, not
    # abs()'d to positive (the mixed-sign-convention case).
    + _txn_line("12/16", "12/15", "CONTOSO MERCHANT ADJUSTMENT", "-15.00") + "\n"
    # Finance-charge-shaped line *within* the charges section -- must be
    # skipped (not added to self.charges), separate from the actual
    # self.finance_charge scalar captured by the line below.
    + _txn_line("12/17", "12/16", "PURCHASE *FINANCE CHARGE* ON PURCHASES", "10.00") + "\n"
    "TOTAL PURCHASES\n"
    "PURCHASE *FINANCE CHARGE* ...................... 10.00\n"
)


def _d(x):
    return Decimal(str(x))


class BofaCreditSyntheticPipelineTest(unittest.TestCase):
    def _parser(self, text=_TEXT, client_name=None):
        p = BankOfAmericaCreditCardParser.__new__(BankOfAmericaCreditCardParser)
        p.client_name = client_name
        p.previous_balance = None
        p.new_balance = None
        p.closing_date = None
        p.payments = []
        p.credits = []
        p.charges = []
        p.finance_charge = None
        p.total_payments = Decimal('0')
        p.text = text
        return p

    def test_balances_and_closing_date_extracted(self):
        p = self._parser()
        p.parse()
        self.assertEqual(p.previous_balance, _d('500.00'))
        self.assertEqual(p.new_balance, _d('290.00'))
        self.assertEqual(p.closing_date, '01/06/26')

    def test_payment_classified_via_keyword_match(self):
        p = self._parser()
        p.parse()
        self.assertEqual(len(p.payments), 1)
        self.assertEqual(p.payments[0]['description'], 'PAYMENT - THANK YOU')
        self.assertEqual(p.payments[0]['amount'], _d('200.00'))

    def test_genuine_credit_within_payments_section(self):
        p = self._parser()
        p.parse()
        self.assertEqual(len(p.credits), 1)
        self.assertEqual(p.credits[0]['description'], 'CONTOSO MERCHANDISE RETURN')
        self.assertEqual(p.credits[0]['amount'], _d('50.00'))

    def test_charge_positive_amount_unchanged(self):
        p = self._parser()
        p.parse()
        vendors = {c['vendor']: c for c in p.charges}
        self.assertIn('CONTOSO OFFICE SUPPLIES', vendors)
        self.assertEqual(vendors['CONTOSO OFFICE SUPPLIES']['amount'], _d('45.00'))

    def test_negative_charge_sign_preserved_not_flipped_positive(self):
        p = self._parser()
        p.parse()
        vendors = {c['vendor']: c for c in p.charges}
        self.assertIn('CONTOSO MERCHANT ADJUSTMENT', vendors)
        self.assertEqual(vendors['CONTOSO MERCHANT ADJUSTMENT']['amount'], _d('-15.00'))

    def test_finance_charge_line_skipped_within_charges_section(self):
        p = self._parser()
        p.parse()
        # Only the two real charges -- the FINANCE CHARGE-labeled line must
        # not appear as a third charge.
        self.assertEqual(len(p.charges), 2)
        self.assertNotIn('PURCHASE *FINANCE CHARGE* ON PURCHASES',
                         {c['vendor'] for c in p.charges})

    def test_finance_charge_scalar_captured_separately(self):
        p = self._parser()
        p.parse()
        self.assertEqual(p.finance_charge, _d('10.00'))

    def test_report_balances(self):
        p = self._parser()
        p.parse()
        report = p.generate_report()
        self.assertIn('Balance verification: PASSED', report)


class BofaCreditsAccountConfigTest(unittest.TestCase):
    """bofa_credits_account (client config) routes the "Payments and Other
    Credits" section into in_credits_section instead of in_payments when
    the account-ending text appears in the preceding lines. Note: today's
    code handles in_payments and in_credits_section identically downstream
    (`if in_payments or in_credits_section:` gates the same classification
    logic) -- this test proves the account-context header detection
    correctly activates state at all, not that the two states behave
    differently (they don't, today)."""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        clients_dir = Path(cls._tmpdir.name)
        cfg = {
            "client_name": "Bravo Studio LLC",
            "canonical_name": "BRAVO STUDIO LLC",
            "aliases": ["Bravo Studio"],
            "statement_types": ["bofa_credit"],
            "bofa_credits_account": "9999",
        }
        (clients_dir / "bravo_studio.json").write_text(json.dumps(cfg))
        cls._previous_base_registry = _base_mod._registry
        cls._previous_bofa_registry = _bofa_mod._registry
        new_registry = ClientRegistry(clients_dir=str(clients_dir))
        _base_mod._registry = new_registry
        _bofa_mod._registry = new_registry

    @classmethod
    def tearDownClass(cls):
        _base_mod._registry = cls._previous_base_registry
        _bofa_mod._registry = cls._previous_bofa_registry
        cls._tmpdir.cleanup()

    def test_credits_account_context_activates_section_state(self):
        text = (
            "December 07, 2025 - January 06, 2026\n"
            "Statement Closing Date ....... 01/06/26\n"
            "Previous Balance ....... $500.00\n"
            "New Balance Total ....... $460.00\n"
            "Account ending in 9999\n"
            "Payments and Other Credits\n"
            + _txn_line("12/10", "12/09", "CONTOSO MERCHANDISE RETURN", "-40.00") + "\n"
            "TOTAL PAYMENTS\n"
        )
        p = BankOfAmericaCreditCardParser.__new__(BankOfAmericaCreditCardParser)
        p.client_name = "Bravo Studio LLC"
        p.previous_balance = None
        p.new_balance = None
        p.closing_date = None
        p.payments = []
        p.credits = []
        p.charges = []
        p.finance_charge = None
        p.total_payments = Decimal('0')
        p.text = text
        p.parse()
        self.assertEqual(len(p.credits), 1)
        self.assertEqual(p.credits[0]['amount'], _d('40.00'))


if __name__ == "__main__":
    unittest.main(verbosity=2)
