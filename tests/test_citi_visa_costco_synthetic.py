"""
test_citi_visa_costco_synthetic.py
------------------------------------
Synthetic regression coverage for CitiVisaCostcoParser, purpose-built as a
companion to tests/dump_report.py for the Extract/Classify/Report pipeline
refactor (see REFACTORING_ROADMAP.md's "Architecture Proposal").

Unlike every other parser migrated in this rollout, the one real fixture
(citi_visa_costco_jojo) provides almost no coverage of the transaction-
extraction loop itself: it's OCR-garbled badly enough that no line ever
reaches _store_transaction() (confirmed directly -- parse() didn't crash
on it even before parsers/citi.py's missing _classify_cc_transaction
import was fixed separately, meaning that call site was simply never
reached). So this synthetic suite is the *primary* verification for this
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

Note: CitiVisaCostcoParser.generate_report() has no balance-verification
block at all (no _balance_check/_is_balanced call), unlike every other
parser in this rollout -- confirmed directly, there's no "Balance
verification: PASSED" line to assert on here.

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
        # No balance-verification block exists on this parser (unlike every
        # other parser in this rollout) -- just confirm the report renders
        # and the key summary figures appear.
        p = self._parser()
        p.parse()
        report = p.generate_report()
        self.assertIn('Previous Balance', report)
        self.assertIn('374.22', report)
        self.assertIn('New Balance', report)
        self.assertIn('446.44', report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
