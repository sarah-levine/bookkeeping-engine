"""
test_citi_checking_synthetic.py
---------------------------------
Synthetic regression coverage for CitiCheckingParser, purpose-built as a
companion to tests/dump_report.py for the Extract/Classify/Report pipeline
refactor (see REFACTORING_ROADMAP.md's "Architecture Proposal").

The real fixture (citi_checking_mp_cheng, confirmed via tests/dump_report.py)
is unusually rich — it already exercises aggregated charges, the
no_aggregate_vendors tag mechanism, checks, ADP payroll, and credit card
payments. This file covers the one real gap: the ACH CREDIT and ELECTRONIC
CREDIT type keywords never appear in that fixture. It also isolates the
ADP -> no-agg-tag -> CREDIT CRD/AUTOPAY -> generic-charge cascade for
ACH DEBIT rows in its own controlled test, independent of real data, so a
regression in that ordering is caught precisely.

Uses a temporary, fictional client config (no real client data) to exercise
the no_aggregate_vendors knob, following the pattern in
tests/test_northern_trust_synthetic.py. Note: parsers/citi.py imports
_registry by bare name (`from parsers.base import ... _registry ...`), the
same gotcha documented there — both parsers.base._registry and
parsers.citi._registry must be patched.
"""
import tempfile
import json
import unittest
from decimal import Decimal
from pathlib import Path

import parsers.base as _base_mod
import parsers.citi as _citi_mod
from parsers.base import ClientRegistry
from parsers.citi import CitiCheckingParser

CANONICAL = "BRAVO STUDIO LLC"

_FICTIONAL_CLIENT_CONFIG = {
    "client_name": "Bravo Studio LLC",
    "canonical_name": CANONICAL,
    "aliases": ["Bravo Studio"],
    "statement_types": ["citi_checking"],
    "no_aggregate_vendors": ["CONTOSO RECURRING"],
}

_TEXT = (
    "Statement Period: May 1 - May 31, 2026\n"
    "Beginning Balance: $68,444.85\n"
    "Ending Balance: $68,114.85\n"
    # ACH CREDIT -> split-on-2+-spaces vendor, lands in credits.
    "05/03 ACH CREDIT SOME CREDIT DESC 500.00 68,944.85\n"
    "  CONTOSO ACH CREDIT SOURCE  EXTRA COLUMN\n"
    # ELECTRONIC CREDIT -> full unsplit vendor-lookahead line, lands in credits.
    "05/06 ELECTRONIC CREDIT SOME DESC 750.00 69,694.85\n"
    "  CONTOSO ELECTRONIC SOURCE  EXTRA COLUMN\n"
    # ACH DEBIT, ADP branch -> adp_transactions.
    "05/07 ACH DEBIT ADP WAGE PAY SOME REF 1200.00 68,494.85\n"
    "  ADP WAGE PAY  EXTRA COLUMN\n"
    # ACH DEBIT, no_aggregate_vendors match -> tagged vendor, lands in charges.
    "05/10 ACH DEBIT RECURRING CHARGE REF 80.00 68,414.85\n"
    "  CONTOSO RECURRING CHARGE  EXTRA COLUMN\n"
    # ACH DEBIT, CREDIT CRD/AUTOPAY match -> credit_card_payments.
    "05/12 ACH DEBIT CARD PAYMENT REF 300.00 68,114.85\n"
    "  CONTOSO CREDIT CRD AUTOPAY  EXTRA COLUMN\n"
)


def _d(x):
    return Decimal(str(x))


class CitiCheckingSyntheticPipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        clients_dir = Path(cls._tmpdir.name)
        (clients_dir / "bravo_studio.json").write_text(json.dumps(_FICTIONAL_CLIENT_CONFIG))
        cls._previous_base_registry = _base_mod._registry
        cls._previous_citi_registry = _citi_mod._registry
        new_registry = ClientRegistry(clients_dir=str(clients_dir))
        _base_mod._registry = new_registry
        _citi_mod._registry = new_registry

    @classmethod
    def tearDownClass(cls):
        _base_mod._registry = cls._previous_base_registry
        _citi_mod._registry = cls._previous_citi_registry
        cls._tmpdir.cleanup()

    def _parser(self):
        p = CitiCheckingParser.__new__(CitiCheckingParser)
        p.client_name = "Bravo Studio LLC"
        p.account_number = ''
        p.statement_date = ''
        p.closing_date = None
        p.previous_balance = Decimal('0')
        p.new_balance = Decimal('0')
        p.payments = []
        p.credits = []
        p.charges = []
        p.adp_transactions = []
        p.credit_card_payments = []
        p.checks = []
        p.total_payments = Decimal('0')
        p.total_credits = Decimal('0')
        p.total_charges = Decimal('0')
        p.total_checks = Decimal('0')
        p.text = _TEXT
        return p

    def test_ach_credit_uses_split_vendor(self):
        p = self._parser()
        p.parse()
        vendors = {c['vendor']: c for c in p.credits}
        self.assertIn('CONTOSO ACH CREDIT SOURCE', vendors)
        self.assertEqual(vendors['CONTOSO ACH CREDIT SOURCE']['amount'], _d('500.00'))

    def test_electronic_credit_uses_full_unsplit_vendor(self):
        p = self._parser()
        p.parse()
        vendors = {c['vendor']: c for c in p.credits}
        # Must be the FULL vendor-lookahead line, double space and all —
        # ELECTRONIC CREDIT is the one credit type that skips the
        # split-on-2+-spaces treatment ACH CREDIT/ACH DEBIT get.
        self.assertIn('CONTOSO ELECTRONIC SOURCE  EXTRA COLUMN', vendors)
        self.assertEqual(vendors['CONTOSO ELECTRONIC SOURCE  EXTRA COLUMN']['amount'], _d('750.00'))

    def test_adp_branch_of_cascade(self):
        p = self._parser()
        p.parse()
        adp_vendors = {a['vendor'] for a in p.adp_transactions}
        self.assertIn('ADP WAGE PAY', adp_vendors)
        self.assertFalse(any('ADP WAGE PAY' in c['vendor'] for c in p.charges),
                          "ADP row must not also land in generic charges")
        self.assertFalse(any('ADP WAGE PAY' in c['vendor'] for c in p.credit_card_payments),
                          "ADP row must not also land in credit_card_payments")

    def test_no_aggregate_vendor_tag_branch_of_cascade(self):
        p = self._parser()
        p.parse()
        # The no_agg tag (vendor|date) must survive onto the charges bucket —
        # stripped only at report render time (report.py's _charges_section).
        tagged_vendors = [c['vendor'] for c in p.charges if c['vendor'].startswith('CONTOSO RECURRING CHARGE|')]
        self.assertEqual(len(tagged_vendors), 1)
        self.assertTrue(tagged_vendors[0].endswith('|05/10/26'))

    def test_credit_crd_autopay_branch_of_cascade(self):
        p = self._parser()
        p.parse()
        cc_vendors = {c['vendor'] for c in p.credit_card_payments}
        self.assertIn('CONTOSO CREDIT CRD AUTOPAY', cc_vendors)
        self.assertFalse(any('CONTOSO CREDIT CRD AUTOPAY' in c['vendor'] for c in p.charges))

    def test_cascade_bucket_counts_exhaustive(self):
        p = self._parser()
        p.parse()
        self.assertEqual(len(p.credits), 2)
        self.assertEqual(len(p.adp_transactions), 1)
        self.assertEqual(len(p.charges), 1)
        self.assertEqual(len(p.credit_card_payments), 1)
        self.assertEqual(len(p.checks), 0)

    def test_total_charges_shared_across_debit_derived_buckets(self):
        # total_charges rolls up ADP + tagged-charge + CC-payment together,
        # matching current (pre-migration) behavior exactly.
        p = self._parser()
        p.parse()
        self.assertEqual(p.total_charges, _d('1200.00') + _d('80.00') + _d('300.00'))

    def test_report_balances(self):
        p = self._parser()
        p.parse()
        report = p.generate_report()
        self.assertIn('Balance verification: PASSED', report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
