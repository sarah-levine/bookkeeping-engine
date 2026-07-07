"""
test_adp_multi_journal_wiring.py
-----------------------------------
Regression coverage for the monkeypatch-and-capture wiring
test_payroll_end_to_end.py uses for adp_payroll_departments and
adp_labor_distribution — the two ADP formats with no separate
_build_journal() to call directly (their journal rows are built inline
inside run_adp_payroll_departments()/run_adp_labor_distribution()).

Real fixtures for these two formats aren't available in every environment
(test_payroll_end_to_end.py's manifest-driven tests skip cleanly without
them), so this uses hand-built synthetic ADP report text — verified against
the real parsing functions — to prove the wiring itself (monkeypatch
_qb_confirm/append_payroll_log/append_digest_log/archive_payroll_pdf/
load_config, call the real run_* function, capture what it logs) produces a
correctly-balanced journal and calls append_payroll_log with the expected
shape. This runs anywhere, including CI — no PDF fixtures needed.
"""
import unittest

import payroll_clients.base as pb_base
from payroll_clients.base import check_balance
import payroll_clients.adp_payroll_departments as depts_mod
import payroll_clients.adp_labor_distribution as labor_mod


DEPARTMENTS_TEXT = """
Checkdate:6/5/2026

Department Total: 100
  1.00           $1,000.00
Total Employees-1

Department Total: 400
  1.00           $500.00
Total Employees-1

Department Total: 600
  1.00           $2,000.00
Total Employees-1

Department Total: 700
  1.00           $300.00
Total Employees-1

Employee:Doe,Jane SSN:XXX-XX-1234
Check Date:6/5/2026/Check/Check No:1001$1,500.00
Check Date:6/5/2026/Direct Deposit/Checking/Account No:XXXX1234$800.00

Pay Frequency Totals: Biweekly
Medical pre-tax 1$50.00
CalSavers Roth$25.00
FEDSOCSEC-ER$100.00
FEDMEDCARE-ER$25.00
FEDFUTA$5.00
CASUI-ER$10.00
"""

DEPARTMENTS_LIABILITY_TEXT = """
Checkdate:6/5/2026
Debit for Taxes $1,565.00
"""

DEPARTMENTS_CFG = {
    "client_name": "Contoso Salon Inc",
    "payroll_key": "contoso_salon",
    "bank_account": "1010 - Checking",
    "employer_tax_account": "6100 - Payroll Taxes",
    "health_insurance_account": "6200 - Health Insurance",
    "departments": {
        "100": {"regular_account": "5100 - Service Wages"},
        "400": {"regular_account": "5400 - Office Wages"},
        "600": {"gross_account": "5600 - Officer Wages"},
        "700": {"gross_account": "5700 - SVW Wages", "contractor_name": ""},
    },
}

LABOR_DIST_TEXT = """
Checkdate:6/5/2026
Check Date: 06/05/2026

Totals for Division: (10)
401K- 100.00
401KLN- 50.00
VSP- 50.00
Total Earnings 1 1,000.00 Total EE Taxes 100.00 Total ER Taxes 80.00 Total Deductions 200.00

Totals for Division: (50)
Total Earnings 1 2,000.00 Total EE Taxes 150.00 Total ER Taxes 160.00 Total Deductions 0.00
"""

LABOR_DIST_CFG = {
    "client_name": "Contoso Salon Inc",
    "payroll_key": "contoso_salon",
    "name_tag": "",
    "admin": {
        "division_code": "10",
        "gross_account": "5100 - Admin Wages",
        "employer_tax_account": "6100 - Admin Taxes",
        "k401_withholding_account": "2100 - 401k Withholding",
        "vsp_account": "2200 - VSP Withholding",
        "clearing_account": "1010 - Clearing",
    },
    "agency": {
        "division_code": "50",
        "gross_account": "5200 - Agency Wages",
        "employer_tax_account": "6200 - Agency Taxes",
        "clearing_account": "1010 - Clearing",
    },
}


class _FakeTextRouter:
    """extract_text() stand-in: maps a fake path to canned text. Needed in
    two places for adp_payroll_departments — the module's own call
    (`text = extract_text(args[0])`) and verify_same_check_date's call
    (defined in payroll_clients.base, resolves via that module's own
    globals) — so both payroll_clients.base.extract_text and
    payroll_clients.adp_payroll_departments.extract_text get patched."""
    def __init__(self, by_path):
        self.by_path = by_path

    def __call__(self, path):
        if path not in self.by_path:
            raise ValueError(f"unexpected path {path!r}")
        return self.by_path[path]


class AdpPayrollDepartmentsWiringTest(unittest.TestCase):
    DETAIL_PATH = "FAKE_DETAIL.pdf"
    LIABILITY_PATH = "FAKE_LIABILITY.pdf"

    def setUp(self):
        router = _FakeTextRouter({
            self.DETAIL_PATH: DEPARTMENTS_TEXT,
            self.LIABILITY_PATH: DEPARTMENTS_LIABILITY_TEXT,
        })
        self._orig = {
            "base.extract_text": pb_base.extract_text,
            "mod.extract_text": depts_mod.extract_text,
            "mod._qb_confirm": depts_mod._qb_confirm,
            "mod.append_payroll_log": depts_mod.append_payroll_log,
            "mod.append_digest_log": depts_mod.append_digest_log,
            "mod.archive_payroll_pdf": depts_mod.archive_payroll_pdf,
            "mod.load_config": depts_mod.load_config,
        }
        pb_base.extract_text = router
        depts_mod.extract_text = router
        depts_mod._qb_confirm = lambda label: True
        depts_mod.archive_payroll_pdf = lambda *a, **k: None
        depts_mod.append_digest_log = lambda *a, **k: None
        depts_mod.load_config = lambda _: DEPARTMENTS_CFG
        self.captured = {}

        def _fake_append_payroll_log(client, client_name, check_date, rows, **kw):
            self.captured["client"] = client
            self.captured["check_date"] = check_date
            self.captured["rows"] = rows

        depts_mod.append_payroll_log = _fake_append_payroll_log

    def tearDown(self):
        pb_base.extract_text = self._orig["base.extract_text"]
        depts_mod.extract_text = self._orig["mod.extract_text"]
        depts_mod._qb_confirm = self._orig["mod._qb_confirm"]
        depts_mod.append_payroll_log = self._orig["mod.append_payroll_log"]
        depts_mod.append_digest_log = self._orig["mod.append_digest_log"]
        depts_mod.archive_payroll_pdf = self._orig["mod.archive_payroll_pdf"]
        depts_mod.load_config = self._orig["mod.load_config"]

    def test_real_function_produces_balanced_journal_and_logs_it(self):
        depts_mod.run_adp_payroll_departments(
            [self.DETAIL_PATH, self.LIABILITY_PATH], "unused.json"
        )
        self.assertEqual(self.captured.get("check_date"), "6/5/2026")
        rows = self.captured.get("rows")
        self.assertTrue(rows, "append_payroll_log was never called")
        debits, credits = check_balance(rows)
        self.assertAlmostEqual(debits, 3940.0, places=2)
        self.assertAlmostEqual(credits, 3940.0, places=2)

    def test_qb_confirm_false_skips_logging(self):
        depts_mod._qb_confirm = lambda label: False
        depts_mod.run_adp_payroll_departments(
            [self.DETAIL_PATH, self.LIABILITY_PATH], "unused.json"
        )
        self.assertNotIn("rows", self.captured, "should not log when QB confirm is declined")


class AdpLaborDistributionWiringTest(unittest.TestCase):
    PDF_PATH = "FAKE_LABOR_DIST.pdf"

    def setUp(self):
        router = _FakeTextRouter({self.PDF_PATH: LABOR_DIST_TEXT})
        self._orig = {
            "base.extract_text": pb_base.extract_text,
            "mod.extract_text": labor_mod.extract_text,
            "mod._qb_confirm": labor_mod._qb_confirm,
            "mod.append_payroll_log": labor_mod.append_payroll_log,
            "mod.append_digest_log": labor_mod.append_digest_log,
            "mod.archive_payroll_pdf": labor_mod.archive_payroll_pdf,
            "mod.load_config": labor_mod.load_config,
        }
        pb_base.extract_text = router
        labor_mod.extract_text = router
        labor_mod._qb_confirm = lambda label: True
        labor_mod.archive_payroll_pdf = lambda *a, **k: None
        labor_mod.append_digest_log = lambda *a, **k: None
        labor_mod.load_config = lambda _: LABOR_DIST_CFG
        self.calls = []

        def _fake_append_payroll_log(client, client_name, check_date, rows, **kw):
            self.calls.append({
                "client": client, "check_date": check_date,
                "rows": rows, "recon_client": kw.get("recon_client"),
            })

        labor_mod.append_payroll_log = _fake_append_payroll_log

    def tearDown(self):
        pb_base.extract_text = self._orig["base.extract_text"]
        labor_mod.extract_text = self._orig["mod.extract_text"]
        labor_mod._qb_confirm = self._orig["mod._qb_confirm"]
        labor_mod.append_payroll_log = self._orig["mod.append_payroll_log"]
        labor_mod.append_digest_log = self._orig["mod.append_digest_log"]
        labor_mod.archive_payroll_pdf = self._orig["mod.archive_payroll_pdf"]
        labor_mod.load_config = self._orig["mod.load_config"]

    def test_both_divisions_logged_separately_and_balanced(self):
        labor_mod.run_adp_labor_distribution([self.PDF_PATH], "unused.json")

        self.assertEqual(len(self.calls), 2, "expected one log entry per division")
        by_client = {c["client"]: c for c in self.calls}

        agency = by_client.get("contoso_salon_agency")
        self.assertIsNotNone(agency, "Agency (Div 50) journal was not logged")
        d, c = check_balance(agency["rows"])
        self.assertAlmostEqual(d, 2160.0, places=2)
        self.assertAlmostEqual(c, 2160.0, places=2)
        self.assertEqual(agency["recon_client"], "contoso_salon")

        admin = by_client.get("contoso_salon_admin")
        self.assertIsNotNone(admin, "Admin (Div 10) journal was not logged")
        d, c = check_balance(admin["rows"])
        self.assertAlmostEqual(d, 1080.0, places=2)
        self.assertAlmostEqual(c, 1080.0, places=2)
        self.assertEqual(admin["recon_client"], "contoso_salon")

    def test_only_agency_confirmed_logs_only_agency(self):
        calls_to_confirm = {"Contoso Salon Inc — Agency (Div 50)": True,
                            "Contoso Salon Inc — Admin (Div 10)": False}
        labor_mod._qb_confirm = lambda label: calls_to_confirm.get(label, False)

        labor_mod.run_adp_labor_distribution([self.PDF_PATH], "unused.json")

        # run_adp_labor_distribution only logs when BOTH are confirmed —
        # partial confirmation must log neither, not just skip one.
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
