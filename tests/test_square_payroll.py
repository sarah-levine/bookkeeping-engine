"""
test_square_payroll.py
-----------------------
Synthetic coverage for payroll_clients.square_payroll — Square Payroll's
"Company Totals Report" xlsx export.

The report repeats the same block layout once for the company-wide total
("All Work Addresses") and again per individual work address (which can
duplicate the same physical address across multiple Square "locations").
parse_workbook() must read only the first (aggregate) block; a real fixture
(needles_studio) has confirmed the per-address blocks sum exactly to the
first block's totals, so reading anything past the first "Total" row would
double either double-count or need extra aggregation logic for no benefit —
QuickBooks only needs one journal entry per check date.

These fixtures are hand-built (fictional numbers/company), not the real
Square export — real-fixture verification against Needles Studio's actual
report happens separately per this repo's testing policy (see CLAUDE.md).
"""
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from payroll_clients.square_payroll import parse_workbook, _build_journal

HEADER = ("Date Range", "Earnings", "Hours", "Pay", "Employee Taxes", "",
          "Employer Taxes", "", "Deductions", "", "Reimbursements", "",
          "EE Benefits Deductions", "", "ER Benefits Contributions", "", "Net Pay")


def _block(from_date, to_date, address_label, rows_after_header):
    """One Date Range block: header + data rows + a trailing Total row."""
    block = [
        HEADER,
        (f"From: {from_date}", "Regular", 10.0, 0, "EE Fed. Income", 0, "ER Fed. Unemployment", 0,
         "", "", "", "", "", "", "", "", ""),
        (f"To: {to_date}", "Overtime", 0.0, 0, "EE Soc. Security", 0, "ER Soc. Security", 0,
         "", "", "", "", "", "", "", "", ""),
        ("", "Double", 0.0, 0, "EE Medicare", 0, "ER Medicare", 0,
         "", "", "", "", "", "", "", "", ""),
        (address_label, "PTO", 0.0, 0, "EE Fed. Additional Medicare", 0,
         "ER CA State Employment Training", 0, "", "", "", "", "", "", "", "", ""),
        ("", "Sick Leave", 0.0, 0, "EE CA State Income", 0, "ER CA State Unemployment", 0,
         "", "", "", "", "", "", "", "", ""),
        ("", "Additional", 0.0, 0, "EE CA State Disability", 0, "", "",
         "", "", "", "", "", "", "", "", ""),
    ]
    block.extend(rows_after_header)
    return block


def _make_workbook(*blocks) -> str:
    wb = Workbook()
    ws = wb.active
    for block in blocks:
        for row in block:
            ws.append(row)
        ws.append([None] * 17)
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    wb.save(tmp.name)
    return tmp.name


def _aggregate_block(from_date="1/2/26", to_date="1/2/26", address_label="All Work Addresses",
                      pay=2000.0, ee_fed=200.0, ee_socsec=124.0, ee_medicare=29.0,
                      ee_ca_income=50.0, ee_ca_disability=20.0,
                      er_fed_unemp=10.0, er_socsec=124.0, er_medicare=29.0,
                      er_ett=2.0, er_sui=60.0, net_pay=1557.0):
    total_ee_tax = round(ee_fed + ee_socsec + ee_medicare + ee_ca_income + ee_ca_disability, 2)
    total_er_tax = round(er_fed_unemp + er_socsec + er_medicare + er_ett + er_sui, 2)
    rows = [
        HEADER,
        (f"From: {from_date}", "Regular", 80.0, pay, "EE Fed. Income", ee_fed,
         "ER Fed. Unemployment", er_fed_unemp, "", "", "", "", "", "", "", "", ""),
        (f"To: {to_date}", "Overtime", 0.0, 0.0, "EE Soc. Security", ee_socsec,
         "ER Soc. Security", er_socsec, "", "", "", "", "", "", "", "", ""),
        ("", "Double", 0.0, 0.0, "EE Medicare", ee_medicare, "ER Medicare", er_medicare,
         "", "", "", "", "", "", "", "", ""),
        (address_label, "PTO", 0.0, 0.0, "EE Fed. Additional Medicare", 0.0,
         "ER CA State Employment Training", er_ett, "", "", "", "", "", "", "", "", ""),
        ("", "Sick Leave", 0.0, 0.0, "EE CA State Income", ee_ca_income,
         "ER CA State Unemployment", er_sui, "", "", "", "", "", "", "", "", ""),
        ("", "Additional", 0.0, 0.0, "EE CA State Disability", ee_ca_disability, "", "",
         "", "", "", "", "", "", "", "", ""),
        ("", "Total", "", pay, "", total_ee_tax, "", total_er_tax,
         "", 0.0, "", 0.0, "", 0.0, "", 0.0, net_pay),
    ]
    return rows


class ParseWorkbookTest(unittest.TestCase):
    def test_reads_only_first_aggregate_block(self):
        # A second, per-address block follows with different numbers —
        # parse_workbook must ignore it and return only the first block's totals.
        agg = _aggregate_block()
        per_address = _aggregate_block(address_label="Work Address: 1 Main St", pay=999.0, net_pay=1.0)
        path = _make_workbook(agg, per_address)
        try:
            parsed = parse_workbook(path)
        finally:
            Path(path).unlink()

        self.assertEqual(parsed["pay"], 2000.0)
        self.assertEqual(parsed["net_pay"], 1557.0)
        self.assertEqual(parsed["employer_taxes"], 225.0)
        self.assertEqual(parsed["check_date"], "01/02/2026")

    def test_tax_categories_bucket_correctly(self):
        agg = _aggregate_block(ee_fed=100.0, ee_socsec=80.0, ee_medicare=20.0,
                                ee_ca_income=25.0, ee_ca_disability=15.0,
                                er_fed_unemp=8.0, er_socsec=80.0, er_medicare=20.0,
                                er_ett=1.0, er_sui=40.0)
        path = _make_workbook(agg)
        try:
            parsed = parse_workbook(path)
        finally:
            Path(path).unlink()

        # IRS = EE(fed+socsec+medicare) + ER(socsec+medicare)
        self.assertAlmostEqual(parsed["ee_irs"] + parsed["er_irs"], 100 + 80 + 20 + 80 + 20)
        # EDD = EE CA income + EE CA disability
        self.assertAlmostEqual(parsed["ee_edd"], 25 + 15)
        # UI/ETT = ER fed unemployment + ER CA ETT + ER CA SUI
        self.assertAlmostEqual(parsed["er_ui_ett"], 8 + 1 + 40)

    def test_multi_day_range_raises(self):
        agg = _aggregate_block(from_date="1/2/26", to_date="1/16/26")
        path = _make_workbook(agg)
        try:
            with self.assertRaises(ValueError):
                parse_workbook(path)
        finally:
            Path(path).unlink()

    def test_nonzero_deduction_raises_instead_of_silently_dropping(self):
        agg = _aggregate_block()
        # Total row: set the Deductions amount (index 9) nonzero.
        total_row = list(agg[-1])
        total_row[9] = 50.00
        agg[-1] = tuple(total_row)
        path = _make_workbook(agg)
        try:
            with self.assertRaises(ValueError):
                parse_workbook(path)
        finally:
            Path(path).unlink()


class BuildJournalTest(unittest.TestCase):
    def test_journal_balances_and_matches_expected_memos(self):
        cfg = {
            "wages_account": "6000 · Salary & Wages",
            "employer_tax_account": "7000 · Expenses:Tax & Lic:Employer Payroll Tax",
            "payroll_bank_account": "1005 · Checking",
        }
        # Internally consistent (mirrors how parse_workbook derives these):
        # net_pay = pay - (ee_irs + ee_edd); employer_taxes = er_irs + er_ui_ett.
        parsed = {
            "pay": 2000.0, "employer_taxes": 225.0, "net_pay": 1780.0,
            "ee_irs": 180.0, "er_irs": 149.0, "ee_edd": 40.0, "er_ui_ett": 76.0,
        }
        rows = _build_journal(cfg, parsed, "01/02/2026")

        total_d = round(sum(float(r["Debit"]) for r in rows if r["Debit"]), 2)
        total_c = round(sum(float(r["Credit"]) for r in rows if r["Credit"]), 2)
        self.assertEqual(total_d, total_c)

        memos = {r["Memo"]: r["Credit"] for r in rows if r["Credit"]}
        self.assertEqual(memos["Netpay"], "1780.00")
        self.assertEqual(memos["EDD"], "40.00")
        self.assertEqual(memos["IRS"], "329.00")
        self.assertEqual(memos["UI/ETT"], "76.00")


if __name__ == "__main__":
    unittest.main()
