"""
test_citi_visa_costco_synthetic.py
------------------------------------
Synthetic regression coverage for CitiVisaCostcoParser, purpose-built as a
companion to tests/dump_report.py for the Extract/Classify/Report pipeline
refactor (see REFACTORING_ROADMAP.md's "Architecture Proposal").

Unlike every other parser migrated in this rollout, the one real fixture
provides almost no coverage of the transaction-extraction loop itself:
it's OCR-garbled badly enough that no line ever reaches
_store_transaction() (confirmed directly -- parse() didn't crash on it
even before parsers/citi.py's missing _classify_cc_transaction import was
fixed separately, meaning that call site was simply never reached). So this synthetic suite is the *primary* verification for this
migration, not a gap-filler -- it covers the inline-amount charge format,
both continuation fallbacks (amount-only, and vendor+amount-with-no-date),
the malformed partial-date payment fallback, payment/credit/charge
classification (including both hardcoded credit-description overrides),
the skip-keyword filter, and one representative fix_ocr_line() repair
(spaced-out single-letter vendor tokens) actually firing through the
migrated extraction path.

The synthetic text below was constructed and verified against the real
(pre-migration, post-import-fix) parser directly, since the OCR-repair-
then-stateful-fallback-chain structure needs exact structural matching.

Note: generate_report() gained a balance-verification block on 2026-07-30
(see REFACTORING_ROADMAP.md), closing the gap with every other parser in
this rollout -- reconcile_comprehensive.py's generic CLI gate previously
couldn't tell a genuine failure from a report that never emits a marker at
all, and always required --force for this statement type as a result. The
`_TEXT` fixture below predates that change and was hand-built purely to
exercise extraction mechanics (each fallback path, one figure at a time) --
its numbers were never made to reconcile against each other, so its own
report shows FAILED. See CitiVisaCostcoBalanceCheckTest further down for
dedicated PASSED/FAILED coverage against figures built to reconcile
(or not) on purpose.

No real client data -- client_name is left unset (None), vendor names use
the Contoso/Acme-style fictional placeholders.
"""
import unittest
from decimal import Decimal

from parsers.citi import CitiVisaCostcoParser

_TEXT = (
    "Billing Period: 03/20/26-04/20/26\n"
    "Previous Balance $374.22\n"
    "New Balance $446.44\n"
    "New Charges $50.00\n"
    "Interest Charged $5.00\n"
    "\n"
    # (a) plain inline charge: date + vendor + amount, no continuation.
    "04/05  Contoso Vendor Supplies  $50.00\n"
    # (b) amount-only continuation: date+vendor on one line, bare amount
    # alone on the next.
    "04/06  Contoso Widget Co\n"
    "$85.00\n"
    # (c) vendor+amount continuation: a bare date line (no vendor text at
    # all), then vendor+amount with NO date on the following line.
    "04/07\n"
    "  Contoso Print Shop   $65.00\n"
    # (d) partial-date payment fallback: malformed MM/D date (not a full
    # MM/DD), negative amount -- routes through the partial_m regex, not
    # the normal date_m path, and always uses the hardcoded
    # 'PAYMENT - THANK YOU' vendor regardless of the line's actual text.
    "04/1 Payment Received -$100.00\n"
    # (e) plain payment via the normal inline path: full date, negative
    # amount, vendor text containing the 'PAYMENT - THANK YOU' keyword.
    "04/08  PAYMENT - THANK YOU  -$100.00\n"
    # (f) Amazon Mktplace credit-description override.
    "04/09  AMAZON MKTPLACE PMTS  -$30.00\n"
    # (f2) Costco return credit-description override.
    "04/10  WWW COSTCO COM  -$40.00\n"
    # (g) plain credit, no override: classifies as credit via the generic
    # 'REFUND' keyword, description stays as the raw vendor text.
    "04/11  Contoso Refund Adjustment Two  $20.00\n"
    # (h) skip-keyword line: excluded entirely, no date needed to trigger it.
    "Minimum Payment Due: $35.00\n"
    # (i) OCR-repair: fix_ocr_line()'s spaced-out-single-letter-token fix
    # collapses "A M A Z O N" -> "AMAZON" before transaction matching runs.
    "04/14  A M A Z O N  $15.00\n"
)


def _d(x):
    return Decimal(str(x))


class CitiVisaCostcoSyntheticPipelineTest(unittest.TestCase):
    def _parser(self):
        p = CitiVisaCostcoParser.__new__(CitiVisaCostcoParser)
        p.client_name = None
        p.previous_balance = Decimal('0')
        p.new_balance = Decimal('0')
        p.total_payments = Decimal('0')
        p.finance_charge = Decimal('0')
        p.payments = []
        p.credits = []
        p.charges = []
        p.closing_date = None
        p.text = _TEXT
        return p

    def test_metadata_and_balances_extracted(self):
        p = self._parser()
        p.parse()
        self.assertEqual(p.closing_date, '04/20/26')
        self.assertEqual(p.previous_balance, _d('374.22'))
        self.assertEqual(p.new_balance, _d('446.44'))
        self.assertEqual(p.statement_new_charges, _d('50.00'))
        self.assertEqual(p.finance_charge, _d('5.00'))

    def test_plain_inline_charge(self):
        p = self._parser()
        p.parse()
        by_vendor = {c['vendor']: c for c in p.charges}
        self.assertIn('Contoso Vendor Supplies', by_vendor)
        self.assertEqual(by_vendor['Contoso Vendor Supplies']['amount'], _d('50.00'))

    def test_amount_only_continuation_fallback(self):
        p = self._parser()
        p.parse()
        by_vendor = {c['vendor']: c for c in p.charges}
        self.assertIn('Contoso Widget Co', by_vendor)
        self.assertEqual(by_vendor['Contoso Widget Co']['amount'], _d('85.00'))

    def test_vendor_and_amount_continuation_fallback(self):
        p = self._parser()
        p.parse()
        by_vendor = {c['vendor']: c for c in p.charges}
        self.assertIn('Contoso Print Shop', by_vendor)
        self.assertEqual(by_vendor['Contoso Print Shop']['amount'], _d('65.00'))

    def test_partial_date_payment_fallback(self):
        p = self._parser()
        p.parse()
        matching = [pmt for pmt in p.payments if pmt['date'] == '04/10/26']
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]['description'], 'PAYMENT - THANK YOU')
        self.assertEqual(matching[0]['amount'], _d('100.00'))

    def test_plain_payment_via_normal_path(self):
        p = self._parser()
        p.parse()
        matching = [pmt for pmt in p.payments if pmt['date'] == '04/08/26']
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]['amount'], _d('100.00'))

    def test_total_payments_scalar_matches_sum_of_payments(self):
        p = self._parser()
        p.parse()
        self.assertEqual(p.total_payments, sum(pmt['amount'] for pmt in p.payments))
        self.assertEqual(p.total_payments, _d('200.00'))

    def test_amazon_mktplace_credit_description_override(self):
        p = self._parser()
        p.parse()
        descs = {c['description']: c for c in p.credits}
        self.assertIn('AMAZON MKTPLACE PMTS', descs)
        self.assertEqual(descs['AMAZON MKTPLACE PMTS']['amount'], _d('30.00'))

    def test_costco_return_credit_description_override(self):
        p = self._parser()
        p.parse()
        descs = {c['description']: c for c in p.credits}
        self.assertIn('COSTCO RETURN', descs)
        self.assertEqual(descs['COSTCO RETURN']['amount'], _d('40.00'))

    def test_plain_credit_no_override_keeps_raw_description(self):
        p = self._parser()
        p.parse()
        descs = {c['description'] for c in p.credits}
        self.assertIn('Contoso Refund Adjustment Two', descs)

    def test_skip_keyword_line_excluded_entirely(self):
        p = self._parser()
        p.parse()
        all_amounts = (
            [pmt['amount'] for pmt in p.payments]
            + [c['amount'] for c in p.credits]
            + [c['amount'] for c in p.charges]
        )
        self.assertNotIn(_d('35.00'), all_amounts)

    def test_ocr_repair_spaced_letters_fires_through_extraction(self):
        p = self._parser()
        p.parse()
        by_vendor = {c['vendor']: c for c in p.charges}
        self.assertIn('AMAZON', by_vendor)
        self.assertEqual(by_vendor['AMAZON']['amount'], _d('15.00'))

    def test_bucket_counts_exhaustive(self):
        p = self._parser()
        p.parse()
        self.assertEqual(len(p.payments), 2)
        self.assertEqual(len(p.credits), 3)
        self.assertEqual(len(p.charges), 4)

    def test_report_generates_without_error(self):
        # This module's _TEXT fixture predates the balance-verification
        # block and was never built to reconcile against itself (see the
        # module docstring) -- just confirm the report renders and the key
        # summary figures appear. See CitiVisaCostcoBalanceCheckTest for
        # PASSED/FAILED coverage.
        p = self._parser()
        p.parse()
        report = p.generate_report()
        self.assertIn('Previous Balance', report)
        self.assertIn('374.22', report)
        self.assertIn('New Balance', report)
        self.assertIn('446.44', report)


_ORPHAN_TEXT = (
    "Billing Period: 06/19/26-07/20/26\n"
    "Previous Balance $446.44\n"
    "New Balance $4,098.16\n"
    "\n"
    # (a) amount-above-date-line displacement (charges): a real
    # pdftotext -layout artifact seen on a two-cardholder statement where a
    # long vendor description line shifts the amount column onto the row
    # above the date+description it actually belongs to, instead of
    # trailing it. Fixed via a forward-lookahead: a bare amount line with no
    # backward-continuation state peeks ahead past blanks for the next
    # date-only line and defers its value there.
    "04/12  Contoso Prior Charge   $19.99\n"
    "                    $77.50\n"
    "04/13  Contoso Utility Co\n"
    "\n"
    # (b) same displacement, but in the Payments/Credits section (negative
    # amount) -- confirms the sign is preserved through the lookahead path,
    # not just the unsigned charge case above.
    "                    -$88.00\n"
    "04/15  ONLINE PAYMENT, THANK YOU\n"
    "\n"
    # (c) partially letter-spaced PAYMENT keyword: only part of the word is
    # OCR/-layout letter-spaced ('PAY M E N T', not every letter), which the
    # old fixed-length-only despacing regex never fired on, and even after
    # despacing can still leave one ordinary-looking space between the
    # two recombined halves of the word. _classify_cc_transaction's
    # whitespace-stripped fallback match is what actually makes this
    # classify correctly, independent of how clean the display text ends up.
    "04/16  ONLINE PAY M E N T, THANK YOU  -$55.00\n"
    "\n"
    # (d) bare AUTOPAY autopay-debit line: never spells out \"PAYMENT\" in
    # full ('AUTOPAY <ref> AUTO-PMT'), and is itself partially letter-spaced
    # ('A U T O PAY'). Needs both the despacing generalization and the
    # explicit AUTOPAY keyword added to _classify_cc_transaction's
    # negative-amount fallback.
    "04/17  A U T O PAY REF12345 AUTO-PMT  -$16.41\n"
    "\n"
    # (e) two-cardholder subtable structure: a standalone NAME header line
    # followed by its own \"Standard Purchases\" header shouldn't bleed
    # state into (or drop) the next cardholder's transactions.
    "CONTOSO CARDHOLDER ONE\n"
    "Standard Purchases\n"
    "04/18  Contoso One Charge   $12.34\n"
    "CONTOSO CARDHOLDER TWO\n"
    "Standard Purchases\n"
    "04/19  Contoso Two Charge   $56.78\n"
)


class CitiVisaCostcoOrphanedAmountAndSpacingTest(unittest.TestCase):
    """Covers the transaction-row bug logged in REFACTORING_ROADMAP.md
    against a real citi_visa_costco statement (closing 07/20/26):
    undercounted charges, dropped payments entirely, even though the
    balance-header half of that same bug (fixed separately in #37) was
    already resolved. No real client data -- fictional Contoso placeholders,
    same convention as the class above."""

    def _parser(self):
        p = CitiVisaCostcoParser.__new__(CitiVisaCostcoParser)
        p.client_name = None
        p.previous_balance = Decimal('0')
        p.new_balance = Decimal('0')
        p.total_payments = Decimal('0')
        p.finance_charge = Decimal('0')
        p.payments = []
        p.credits = []
        p.charges = []
        p.closing_date = None
        p.text = _ORPHAN_TEXT
        return p

    def test_amount_above_date_line_charge(self):
        p = self._parser()
        p.parse()
        by_vendor = {c['vendor']: c for c in p.charges}
        self.assertIn('Contoso Utility Co', by_vendor)
        self.assertEqual(by_vendor['Contoso Utility Co']['amount'], _d('77.50'))
        # The unrelated charge before it must still parse normally --
        # confirms the lookahead didn't consume the wrong amount.
        self.assertIn('Contoso Prior Charge', by_vendor)
        self.assertEqual(by_vendor['Contoso Prior Charge']['amount'], _d('19.99'))

    def test_amount_above_date_line_payment_preserves_sign(self):
        p = self._parser()
        p.parse()
        matching = [pmt for pmt in p.payments if pmt['date'] == '04/15/26']
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]['amount'], _d('88.00'))

    def test_partially_spaced_payment_keyword_still_classifies_as_payment(self):
        p = self._parser()
        p.parse()
        matching = [pmt for pmt in p.payments if pmt['date'] == '04/16/26']
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]['amount'], _d('55.00'))

    def test_bare_autopay_line_classifies_as_payment_not_credit(self):
        p = self._parser()
        p.parse()
        matching = [pmt for pmt in p.payments if pmt['date'] == '04/17/26']
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]['amount'], _d('16.41'))
        # Must not have landed in credits instead (the pre-fix behavior).
        credit_dates = [c['date'] for c in p.credits]
        self.assertNotIn('04/17/26', credit_dates)

    def test_two_cardholder_sections_both_captured(self):
        p = self._parser()
        p.parse()
        by_vendor = {c['vendor']: c for c in p.charges}
        self.assertIn('Contoso One Charge', by_vendor)
        self.assertEqual(by_vendor['Contoso One Charge']['amount'], _d('12.34'))
        self.assertIn('Contoso Two Charge', by_vendor)
        self.assertEqual(by_vendor['Contoso Two Charge']['amount'], _d('56.78'))


class CitiVisaCostcoBalanceCheckTest(unittest.TestCase):
    """Dedicated coverage for the balance-verification block added
    2026-07-30 (see REFACTORING_ROADMAP.md and CLAUDE.md's balance-check
    gate in reconcile_comprehensive.py, which previously could never pass
    for this statement type -- the report never emitted a marker at all).
    Builds parser state directly rather than through parse(), so the
    figures can be constructed to reconcile (or deliberately not) on
    purpose, independent of any real or synthetic statement text."""

    def _parser(self, previous_balance, total_payments, credit_amount,
                charge_amount, finance_charge, new_balance):
        p = CitiVisaCostcoParser.__new__(CitiVisaCostcoParser)
        p.client_name = None
        p.closing_date = '04/20/26'
        p.statement_date = ''
        p.previous_balance = _d(previous_balance)
        p.new_balance = _d(new_balance)
        p.total_payments = _d(total_payments)
        p.finance_charge = _d(finance_charge)
        p.statement_new_charges = Decimal('0')
        p.payments = ([{'date': '04/05/26', 'description': 'PAYMENT - THANK YOU',
                         'amount': _d(total_payments)}] if _d(total_payments) else [])
        p.credits = ([{'date': '04/06/26', 'description': 'Contoso Refund',
                        'amount': _d(credit_amount)}] if _d(credit_amount) else [])
        p.charges = ([{'date': '04/07/26', 'vendor': 'Contoso Vendor',
                        'amount': _d(charge_amount)}] if _d(charge_amount) else [])
        return p

    def test_passes_when_figures_reconcile(self):
        # 100.00 - 20.00 payments - 5.00 credits + 30.00 charges = 105.00
        p = self._parser('100.00', '20.00', '5.00', '30.00', '0.00', '105.00')
        report = p.generate_report()
        self.assertIn('✓ Balance verification: PASSED', report)

    def test_fails_when_figures_dont_reconcile(self):
        p = self._parser('100.00', '20.00', '5.00', '30.00', '0.00', '999.00')
        report = p.generate_report()
        self.assertIn('✗ Balance verification: FAILED', report)

    def test_fails_when_extraction_undercounts_charges(self):
        # Mirrors the real bug this whole fix addresses: extraction that
        # silently drops a transaction must show FAILED, not a report with
        # no marker at all that a CLI gate can't tell apart from success.
        p = self._parser('446.44', '1776.47', '0.00', '5158.17', '0.00', '4098.16')
        report = p.generate_report()
        self.assertIn('✗ Balance verification: FAILED', report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
