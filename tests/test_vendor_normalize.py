"""Vendor/description normalization regression tests.

Locks in the consolidated normalization behavior:
  • one STANDARD cleaner (parsers.vendor_normalize.auto_clean_vendor),
  • one CLIENT tier (ClientRegistry.normalize_vendor / clean_and_normalize),
  • no duplicate copies, and no client-specific literals hardcoded in the
    standard cleaner (the former "SAN JOSE" strip now comes from config).
These import cleanly without any PDF dependencies.
"""
import unittest

from parsers import vendor_normalize as vn
from parsers.base import (
    _registry,
    _auto_clean_vendor,
    _collect_unknown_vendors,  # noqa: F401  (presence = no duplicate defs)
)
import reconcile_comprehensive as rc


class StandardCleaner(unittest.TestCase):
    def test_generic_noise_stripped(self):
        cases = {
            "TST* SOME RESTAURANT 800-555-1234 CA": "Some Restaurant",
            "SQ *COFFEE SHOP SJC": "Coffee Shop",
            "WHSE#1267": "Whse",
            "SOME VENDOR ECOMM": "Some Vendor",
            "VENDOR ST5920": "Vendor",
            "MDC*PARKING LOT NJ": "Parking Lot",
            "PLAIN VENDOR NAME": "Plain Vendor Name",
        }
        for raw, expected in cases.items():
            display, _ = vn.auto_clean_vendor(raw)
            self.assertEqual(display, expected, raw)

    def test_mixed_case_preserved(self):
        display, _ = vn.auto_clean_vendor("PRIMO BRANDS/WATERSERV")
        self.assertEqual(display, "Primo Brands/waterserv")

    def test_contains_key_is_upper(self):
        _, key = vn.auto_clean_vendor("Some Vendor ECOMM")
        self.assertEqual(key, "SOME VENDOR")


class StandardLocationStrip(unittest.TestCase):
    """The standard cleaner strips generic '<CITY> <ST>' tails algorithmically
    (no hardcoded city names), when a store-number/reference boundary separates
    the city from the vendor name."""

    def test_city_state_stripped_with_store_number_boundary(self):
        cases = {
            "CONTOSO WHSE#1267 EXAMPLE CITY CA": "Contoso Whse",
            "FABRIKAM #2900 EXAMPLE CITY CA": "Fabrikam",
            "ACME COFFEE #345 EXAMPLE CITY CA": "Acme Coffee",
        }
        for raw, expected in cases.items():
            display, _ = vn.auto_clean_vendor(raw)
            self.assertEqual(display, expected, raw)

    def test_strip_is_algorithmic_not_a_literal(self):
        # A different placeholder city is stripped too -> proves there's no
        # hardcoded city literal in the cleaner; it's anchored on <CITY> <ST>.
        display, _ = vn.auto_clean_vendor("CONTOSO #123 OTHER TOWN CA")
        self.assertEqual(display, "Contoso")

    def test_vendor_words_not_eaten_without_boundary(self):
        # No store-number boundary -> can't tell city from vendor, so the city
        # is left in place rather than risk eating vendor words.
        display, _ = vn.auto_clean_vendor("ACME STORE EXAMPLE CITY TX")
        self.assertEqual(display, "Acme Store Example City")

    def test_multiword_vendor_with_state_but_no_city(self):
        # Phone precedes the state -> no alpha city candidate, nothing eaten.
        display, _ = vn.auto_clean_vendor("CONTOSO PAY ONLINE 800-464-4000 CA")
        self.assertEqual(display, "Contoso Pay Online")


class ClientSuffixesFromConfig(unittest.TestCase):
    """`description_strip_suffixes` still works for tails the generic strip
    can't catch (no store-number boundary) or non-location tokens."""

    def test_city_stripped_when_configured(self):
        # No store-number boundary, so the generic strip conservatively leaves
        # the city; the client's description_strip_suffixes removes it.
        plain, _ = vn.auto_clean_vendor("ACME STORE EXAMPLE CITY TX")
        self.assertEqual(plain, "Acme Store Example City")
        configured, _ = vn.auto_clean_vendor(
            "ACME STORE EXAMPLE CITY TX", ["EXAMPLE CITY"])
        self.assertEqual(configured, "Acme Store")

    def test_strip_client_suffixes_helper(self):
        self.assertEqual(
            vn.strip_client_suffixes("ACME CORP DOWNTOWN", ["DOWNTOWN"]),
            "ACME CORP")
        self.assertEqual(vn.strip_client_suffixes("ACME CORP", None), "ACME CORP")


class ClientRules(unittest.TestCase):
    def test_example_client_vendor_rules(self):
        cases = {
            "COMCAST CABLE 1234": "Comcast/Xfinity",
            "PGANDE WEB ONLINE": "PG&E",
            "AMAZON MKTP": "Amazon",
            "STAPLES OFFICE 123": "Staples - Office Supplies",
            "INTEREST CHARGE": "Interest Charge (Amex)",
        }
        for raw, expected in cases.items():
            self.assertEqual(_registry.normalize_vendor("ACME INC", raw),
                             expected, raw)

    def test_unmatched_passthrough(self):
        self.assertEqual(
            _registry.normalize_vendor("ACME INC", "TOTALLY UNMATCHED VENDOR"),
            "TOTALLY UNMATCHED VENDOR")

    def test_starts_with_only_rule_fires(self):
        # Regression: a vendor_rule with starts_with but no contains used to be
        # skipped entirely. example_client has {"starts_with": "USPS", ...}.
        self.assertEqual(
            _registry.normalize_vendor("ACME INC", "USPS SHIPPING 1234"), "USPS")

    def test_clean_and_normalize_pipeline(self):
        # description_strip_suffixes in example_client.json strips "EXAMPLE CITY"
        # before vendor_rules match.
        self.assertEqual(
            _registry.clean_and_normalize("ACME INC", "AMAZON MKTP EXAMPLE CITY"),
            "Amazon")


class NoDuplication(unittest.TestCase):
    def test_single_source_of_truth(self):
        # base.py and reconcile_comprehensive.py reference the SAME objects,
        # not divergent copies.
        self.assertIs(_auto_clean_vendor, vn.auto_clean_vendor)
        self.assertIs(rc._auto_clean_vendor, _auto_clean_vendor)
        self.assertIs(rc._collect_unknown_vendors, _collect_unknown_vendors)


if __name__ == "__main__":
    unittest.main()
