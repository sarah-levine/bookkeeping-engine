"""
test_row_schema.py
-------------------
Isolated shape validation for parsers/row_schema.py's TransactionRow. No
parser consumes this module yet (see the module docstring) — this test only
proves the dataclass holds the fields/types the Extract/Classify/Report
pipeline refactor will rely on.
"""
import unittest
from decimal import Decimal

from parsers.row_schema import TransactionRow


class TransactionRowTest(unittest.TestCase):
    def test_construction_with_all_fields(self):
        row = TransactionRow(
            date="06/03/26",
            vendor="Contoso Widgets Inc",
            raw_description="CONTOSO WIDGETS INC PURCHASE",
            amount=Decimal("-40.00"),
            type="debit",
        )
        self.assertEqual(row.date, "06/03/26")
        self.assertEqual(row.vendor, "Contoso Widgets Inc")
        self.assertEqual(row.raw_description, "CONTOSO WIDGETS INC PURCHASE")
        self.assertEqual(row.amount, Decimal("-40.00"))
        self.assertEqual(row.type, "debit")

    def test_sign_convention_debit_is_negative(self):
        row = TransactionRow("06/03/26", "Contoso", "CONTOSO", Decimal("-40.00"), "debit")
        self.assertLess(row.amount, 0)

    def test_sign_convention_credit_is_positive(self):
        row = TransactionRow("06/03/26", "Contoso", "CONTOSO", Decimal("40.00"), "credit")
        self.assertGreater(row.amount, 0)

    def test_all_row_types_constructible(self):
        for row_type in ("credit", "debit", "check", "payment", "fee"):
            row = TransactionRow("06/03/26", "Contoso", "CONTOSO", Decimal("1.00"), row_type)
            self.assertEqual(row.type, row_type)


if __name__ == "__main__":
    unittest.main(verbosity=2)
