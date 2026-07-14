"""
test_northern_trust_synthetic.py
---------------------------------
Synthetic regression coverage for NorthernTrustCheckingParser, purpose-built
as a companion to tests/dump_report.py for the Extract/Classify/Report
pipeline refactor (see REFACTORING_ROADMAP.md's "Architecture Proposal").

Why this exists: the one real Northern Trust fixture in
tests/fixtures_manifest.json (northern_trust_checking_needles) has zero
CC-payment lines and no Square-position-mapped transactions — confirmed by
running tests/dump_report.py against it. A real-fixture byte-diff alone
therefore cannot prove a refactor preserved CC-payment classification or the
config-driven Square line-position mapping, both of which live in
parse()'s classification logic today. This file exercises both paths (plus
a plain credit and a plain debit) against a fictional, self-contained client
config, so it's safe to commit (no real client data — see CLAUDE.md's
public-repo-hygiene rule) and independent of Drive credentials.

Run this file's tests before and after each phase of the parser refactor —
alongside tests/dump_report.py's real-fixture diff — to catch a regression
neither one alone would.
"""
import tempfile
import json
import unittest
from decimal import Decimal
from pathlib import Path

import parsers.base as _base_mod
import parsers.northern_trust as _nt_mod
from parsers.base import ClientRegistry
from parsers.northern_trust import NorthernTrustCheckingParser
from tests.dump_report import _normalize

CANONICAL = "BRAVO STUDIO LLC"

_FICTIONAL_CLIENT_CONFIG = {
    "client_name": "Bravo Studio LLC",
    "canonical_name": CANONICAL,
    "aliases": ["Bravo Studio"],
    "statement_types": ["northern_trust_checking"],
    "cc_keywords": ["CONTOSO CARD"],
    "square_line_order": [
        {"position": 1, "account": "Square Deposit - Bravo", "memo": "Weekly Square settlement"},
    ],
}

# Two-line-per-transaction shape Northern Trust's parse() expects: a primary
# line ("ACH Debit|ACH Credit|Deposit|Withdrawal <desc> <amount>") followed by
# a continuation line supplying the MM/DD date.
_TEXT = (
    "Statement Period\n06/01/26 through 06/30/26\n"
    "Beginning Balance on 06/01/26  5,000.00\n"
    "Other Items Paid\n"
    # Generic card-network fallback (_is_known_cc_network_payment) -> CC payment
    "ACH Debit AMERICAN EXPRESS ACH PMT 3900.00\n"
    "REF001 06/03 8797001 CCD\n"
    # Client-config cc_keywords fallback -> CC payment
    "ACH Debit CONTOSO CARD ONLINE PMT 500.00\n"
    "REF002 06/05 8797002 CCD\n"
    # Plain debit, no CC classification
    "ACH Debit CONTOSO WIDGETS INC 40.00\n"
    "REF003 06/07 8797003 CCD\n"
    # Plain credit
    "Deposit CONTOSO CUSTOMER PAYMENT 250.00\n"
    "REF004 06/10 8797004 CCD\n"
    # Square #1 -> mapped via square_line_order position 1
    "ACH Debit Square Inc SQ250303 T3QXZF 55.00\n"
    "REF005 06/12 8797005 CCD\n"
    # Square #2 -> no position-2 mapping configured, falls through as a plain debit
    "ACH Debit Square Inc SQ250310 T9ZQXF 65.00\n"
    "REF006 06/14 8797006 CCD\n"
    "Daily Ledger\n"
    "Ending Balance on 06/30/26, 2026  690.00\n"
)


def _d(x):
    return Decimal(str(x))


class NorthernTrustSyntheticPipelineTest(unittest.TestCase):
    """Builds a parser via __new__ (bypassing PDF/OCR extraction), feeding it
    the synthetic text above, matching the pattern already used in
    tests/test_cc_payment_classification.py::NorthernTrustCCClassificationTest."""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        clients_dir = Path(cls._tmpdir.name)
        (clients_dir / "bravo_studio.json").write_text(json.dumps(_FICTIONAL_CLIENT_CONFIG))
        # northern_trust.py does `from parsers.base import _registry` (a bare
        # name import, binding its own module-level reference at import time)
        # rather than looking up parsers.base._registry dynamically — so
        # patching parsers.base._registry alone is invisible to parse().
        # Same gotcha REFACTORING_ROADMAP.md documents for bmo.py; patch both.
        cls._previous_base_registry = _base_mod._registry
        cls._previous_nt_registry = _nt_mod._registry
        new_registry = ClientRegistry(clients_dir=str(clients_dir))
        _base_mod._registry = new_registry
        _nt_mod._registry = new_registry

    @classmethod
    def tearDownClass(cls):
        _base_mod._registry = cls._previous_base_registry
        _nt_mod._registry = cls._previous_nt_registry
        cls._tmpdir.cleanup()

    def _parser(self):
        p = NorthernTrustCheckingParser.__new__(NorthernTrustCheckingParser)
        p.client_name = "Bravo Studio LLC"
        p.credits = []
        p.debits = []
        p.credit_card_payments = []
        p.checks = []
        p.beginning_balance = None
        p.ending_balance = None
        p.closing_date = None
        p.text = _TEXT
        return p

    def test_cc_payments_classified_via_generic_and_client_fallback(self):
        p = self._parser()
        p.parse()
        self.assertEqual(len(p.credit_card_payments), 2)
        vendors = {c['vendor'] for c in p.credit_card_payments}
        self.assertIn('AMERICAN EXPRESS ACH PMT', vendors)
        self.assertIn('CONTOSO CARD ONLINE PMT', vendors)
        amounts = {c['vendor']: c['amount'] for c in p.credit_card_payments}
        self.assertEqual(amounts['AMERICAN EXPRESS ACH PMT'], _d(-3900))
        self.assertEqual(amounts['CONTOSO CARD ONLINE PMT'], _d(-500))

    def test_plain_debit_not_misclassified(self):
        p = self._parser()
        p.parse()
        debit_vendors = {d['vendor'] for d in p.debits}
        self.assertIn('CONTOSO WIDGETS INC', debit_vendors)
        self.assertNotIn('CONTOSO WIDGETS INC', {c['vendor'] for c in p.credit_card_payments})

    def test_plain_credit_classified(self):
        p = self._parser()
        p.parse()
        self.assertEqual(len(p.credits), 1)
        self.assertEqual(p.credits[0]['vendor'], 'CONTOSO CUSTOMER PAYMENT')
        self.assertEqual(p.credits[0]['amount'], _d(250))

    def test_square_position_mapping_applied_to_first_match_only(self):
        p = self._parser()
        p.parse()
        debit_vendors = [d['vendor'] for d in p.debits]
        # First Square transaction remapped per square_line_order position 1.
        self.assertIn('Square Deposit - Bravo', debit_vendors)
        mapped = next(d for d in p.debits if d['vendor'] == 'Square Deposit - Bravo')
        self.assertEqual(mapped['memo'], 'Weekly Square settlement')
        self.assertEqual(mapped['amount'], _d(-55))
        # Second Square transaction has no position-2 mapping -> falls through
        # as a plain debit, vendor text unchanged.
        self.assertIn('Square Inc SQ250310 T9ZQXF', debit_vendors)
        # Neither Square transaction is a CC payment.
        self.assertFalse(any('Square' in c['vendor'] for c in p.credit_card_payments))

    def test_debit_and_credit_counts_are_exhaustive(self):
        # 6 transactions total: 2 CC payments, 3 plain debits (Contoso Widgets,
        # mapped Square, unmapped Square), 1 credit.
        p = self._parser()
        p.parse()
        self.assertEqual(len(p.credit_card_payments), 2)
        self.assertEqual(len(p.debits), 3)
        self.assertEqual(len(p.credits), 1)

    def test_report_balances(self):
        p = self._parser()
        p.parse()
        report = _normalize(p.generate_report())
        self.assertIn('Balance verification: PASSED', report)
        self.assertIn('Generated: <TIMESTAMP>', report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
