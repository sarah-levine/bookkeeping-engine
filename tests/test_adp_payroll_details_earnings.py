"""
test_adp_payroll_details_earnings.py
---------------------------------------
Regression coverage for payroll_clients.adp_payroll_details.parse_associates_earnings().

Previously the Associates (dept 002) earnings block only summed labels it
explicitly regex-matched (Regular, Overtime, RestTime, Commission, Sick) —
any other ADP earnings category (Holiday, Bonus, Vacation, etc.) silently
vanished from assoc_gross, throwing the journal entry out of balance with
no indication why. This happened for real with "Sick" until 2026-07-02
(a client's run came up $143.60 out of balance).

Now earnings lines ("Label  <hours>  $<amount>") are summed generically —
an unrecognized label is still included in assoc_gross (via assoc["other"])
with a printed note, instead of silently dropping out.
"""
import io
import unittest
from contextlib import redirect_stdout

from payroll_clients.adp_payroll_details import parse_associates_earnings


def _block(*earnings_lines):
    return ["DepartmentTotals:002-Associates", *earnings_lines, "TotalEmployees-002"]


class ParseAssociatesEarningsTest(unittest.TestCase):
    def test_known_categories_regression(self):
        lines = _block(
            "Regular 40.00 $1,000.00",
            "Overtime 5.00 $150.00",
            "RestTime 2.00 $50.00",
            "Commission 0.00 $25.00",
            "Sick 8.00 $200.00",
        )
        assoc, gross = parse_associates_earnings(lines)
        self.assertEqual(assoc["regular"], 1000.0)
        self.assertEqual(assoc["overtime"], 150.0)
        self.assertEqual(assoc["rest"], 50.0)
        self.assertEqual(assoc["commission"], 25.0)
        self.assertEqual(assoc["sick"], 200.0)
        self.assertEqual(assoc["other"], 0)
        self.assertEqual(gross, 1425.0)

    def test_unrecognized_category_is_included_not_dropped(self):
        # The actual bug class: a brand-new ADP category (here "Holiday",
        # standing in for the real "Sick" incident) must not silently
        # vanish from assoc_gross.
        lines = _block("Regular 40.00 $1,000.00", "Holiday 8.00 $300.00")
        buf = io.StringIO()
        with redirect_stdout(buf):
            assoc, gross = parse_associates_earnings(lines)
        self.assertEqual(assoc["other"], 300.0)
        self.assertEqual(gross, 1300.0, "unrecognized category must still be included in gross")
        self.assertIn("Holiday", buf.getvalue())
        self.assertIn("not previously seen", buf.getvalue())

    def test_multiple_unrecognized_categories_all_summed(self):
        lines = _block(
            "Regular 40.00 $1,000.00",
            "Holiday 8.00 $300.00",
            "Bonus 0.00 $500.00",
        )
        assoc, gross = parse_associates_earnings(lines)
        self.assertEqual(assoc["other"], 800.0)
        self.assertEqual(gross, 1800.0)

    def test_tips_excluded_from_gross_but_still_tracked(self):
        lines = _block(
            "Regular 40.00 $1,000.00",
            "QualifiedTipPaid* 0.00 $75.00",
        )
        assoc, gross = parse_associates_earnings(lines)
        self.assertEqual(assoc["tips"], 75.0)
        self.assertEqual(gross, 1000.0, "tips must not be included in assoc_gross")

    def test_empty_block_returns_zero_gross(self):
        lines = _block()
        assoc, gross = parse_associates_earnings(lines)
        self.assertEqual(gross, 0)

    def test_lines_outside_the_block_are_ignored(self):
        lines = [
            "Regular 999.00 $9,999.00",  # before the block starts — ignored
            *_block("Regular 40.00 $1,000.00"),
            "Regular 999.00 $9,999.00",  # after the block ends — ignored
        ]
        assoc, gross = parse_associates_earnings(lines)
        self.assertEqual(gross, 1000.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
