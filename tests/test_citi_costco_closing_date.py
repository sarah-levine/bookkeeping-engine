"""
test_citi_costco_closing_date.py
------------------------------------
Regression coverage for CitiVisaCostcoParser._extract_closing_date().

Previously this only tried a single per-line regex requiring a clean,
single-space "Billing Period: MM/DD/YY-MM/DD/YY" on one line — any OCR
noise silently left closing_date unset. Confirmed against a real scanned
Citi Costco fixture where the billing-period text OCR'd to "Billing
Period: O3/2O//6-O4/2dt26" (a dropped digit and stray letters, not just
simple digit confusion) — unrecoverable even with added OCR-digit
tolerance. The same closing date OCR'd cleanly a few lines later as
"$209.49 as of 04/20/26", which is now the fallback source.

Parser is instantiated via __new__ (bypassing PDF extraction) and fed the
real fixture's text directly (captured as a literal string below) — no PDF
fixture or Drive access needed to run this.
"""
import unittest

from parsers.citi import CitiVisaCostcoParser


def _parser(text):
    p = CitiVisaCostcoParser.__new__(CitiVisaCostcoParser)
    p.text = text
    return p


class ClosingDateExtractionTest(unittest.TestCase):
    def test_clean_single_line_billing_period(self):
        p = _parser("Some header\nBilling Period: 12/19/25-01/20/26\nmore text")
        self.assertEqual(p._extract_closing_date(), "01/20/26")

    def test_billing_period_split_across_a_line_break(self):
        p = _parser("Some header\nBilling Period:\n12/19/25-01/20/26\nmore text")
        self.assertEqual(p._extract_closing_date(), "01/20/26")

    def test_billing_period_with_irregular_whitespace(self):
        p = _parser("Billing   Period :  12/19/25 -  01/20/26")
        self.assertEqual(p._extract_closing_date(), "01/20/26")

    def test_billing_period_with_mild_ocr_digit_confusion(self):
        # O for 0, l for 1 — the character-level confusion _OCR_DIGIT targets.
        p = _parser("Billing Period: l2/l9/25-Ol/2O/26")
        self.assertEqual(p._extract_closing_date(), "01/20/26")

    def test_real_severely_garbled_fixture_falls_back_to_as_of_date(self):
        # Verbatim extracted text from a real scanned Citi Costco fixture —
        # the billing-period digits are unrecoverable (dropped digit, stray
        # letters "dt"), but the "as of" fallback recovers the same date.
        text = (
            "Account Ending in: 3003\n"
            "APRIL STATEMENT .-- Costco Cash Back Rewards Summary\n"
            "Billing Period: O3/2O//6-O4/2dt26\n"
            "Total Cash back:\n"
            "New balance as $227.74\n"
            "$209.49 as of 04/20/26\n"
            "Minimum payment di $41.00\n"
        )
        p = _parser(text)
        self.assertEqual(p._extract_closing_date(), "04/20/26")

    def test_no_billing_period_or_as_of_date_returns_none(self):
        p = _parser("Some unrelated statement text with no billing period info")
        self.assertIsNone(p._extract_closing_date())


if __name__ == "__main__":
    unittest.main(verbosity=2)
