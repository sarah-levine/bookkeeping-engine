"""
test_wells_fargo_checking_agg_synthetic.py
--------------------------------------------
Regression coverage for WellsFargoCheckingParser.generate_report() using
the shared _aggregate_by_vendor() (parsers/base.py) instead of a local
agg() reimplementation (see REFACTORING_ROADMAP.md's Wells Fargo Credit
Card status entry for the full comparison — the local version collapsed by
vendor name alone with no month bucketing, skipped normalize_vendor(), and
sorted by a bare string compare instead of a parsed date).

This file proves the actual behavior gain the fix was for: the same
vendor appearing in two different months within one statement now renders
as two separate aggregated rows, not one collapsed row — needed for any
future client whose statement period spans a month boundary or who has
recurring same-vendor debits across months. Also locks in the Zelle
normalize_vendor() idempotency fix (parsers/wells_fargo.py's _normalize())
required to make double-normalization (flush() at parse time, then again
inside _aggregate_by_vendor()) safe.

No real client data — client_name is left unset (None).
"""
import unittest
from decimal import Decimal

from parsers.wells_fargo import WellsFargoCheckingParser


def _d(x):
    return Decimal(str(x))


class WellsFargoCheckingAggSyntheticTest(unittest.TestCase):
    def _parser(self, debits=None, credits=None):
        p = WellsFargoCheckingParser.__new__(WellsFargoCheckingParser)
        p.client_name = None
        p.beginning_balance = Decimal('1000.00')
        p.ending_balance = Decimal('875.00')
        p.statement_period = 'January 31, 2026'
        p.closing_date = '01/31/26'
        p.credits = credits or []
        p.debits = debits or []
        p.checks = []
        p.bank_fees = []
        p.credit_card_payments = []
        return p

    def test_same_vendor_different_months_renders_as_two_rows(self):
        p = self._parser(debits=[
            {'date': '01/05', 'vendor': 'Contoso Recurring Vendor', 'amount': _d('50.00')},
            {'date': '02/10', 'vendor': 'Contoso Recurring Vendor', 'amount': _d('75.00')},
        ])
        report = p.generate_report()
        # Two separate lines, each unaggregated (no "(2)" count suffix),
        # not one collapsed "Contoso Recurring Vendor (2)" line.
        self.assertIn('01/05        Contoso Recurring Vendor', report)
        self.assertIn('02/10        Contoso Recurring Vendor', report)
        self.assertNotIn('Contoso Recurring Vendor (2)', report)
        self.assertIn('50.00', report)
        self.assertIn('75.00', report)

    def test_same_vendor_same_month_still_aggregates(self):
        p = self._parser(debits=[
            {'date': '01/05', 'vendor': 'Contoso Recurring Vendor', 'amount': _d('50.00')},
            {'date': '01/20', 'vendor': 'Contoso Recurring Vendor', 'amount': _d('75.00')},
        ])
        report = p.generate_report()
        self.assertIn('Contoso Recurring Vendor (2)', report)
        self.assertIn('125.00', report)

    def test_dates_render_without_spurious_year_suffix(self):
        # date_fmt='%m/%d' must be passed explicitly to _aggregate_by_vendor()
        # -- the default '%m/%d/%y' would still parse WF's dateless strings
        # via the fallback chain but would render a spurious "/00" suffix.
        p = self._parser(debits=[
            {'date': '01/05', 'vendor': 'Contoso Recurring Vendor', 'amount': _d('50.00')},
        ])
        report = p.generate_report()
        self.assertIn('01/05 ', report)
        self.assertNotIn('01/05/00', report)

    def test_zelle_to_name_survives_double_normalization(self):
        # Regression for the bug this fix's initial attempt introduced:
        # flush() normalizes once at parse time ("Zelle to Jane Doe on
        # 01/27/26 ..." -> "Zelle to Jane Doe"), then _aggregate_by_vendor()
        # normalizes again. Before the _normalize() idempotency fix, the
        # second pass's regex required a trailing " on" that no longer
        # existed, silently degrading the name to generic "Zelle Payment".
        p = self._parser(debits=[
            {'date': '01/27', 'vendor': 'Zelle to Jane Doe', 'amount': _d('500.00')},
        ])
        report = p.generate_report()
        self.assertIn('Zelle to Jane Doe', report)
        self.assertNotIn('Zelle Payment', report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
