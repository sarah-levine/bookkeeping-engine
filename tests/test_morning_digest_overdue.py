"""
test_morning_digest_overdue.py
-------------------------------
Regression coverage for send_morning_digest.py's "current vs. overdue"
classification (acct_group / is_reconciliation_current), shared by the
tracker-card badges and the "Overdue — Not Yet Reconciled" email section.

Bug this guards against: the overdue section used to compare a reconciled
statement's (year, month) directly against today's (year, month), with no
regard for account closing-cycle shape. Checking/savings accounts close on
the last day of the month (EOM) — reconciled through last month's close, they
were flagged "overdue" for the new month the instant the calendar month
ticked over, days or weeks before that new month's statement had even
closed. Credit cards close mid-month on a fixed day and need the opposite
check (last_date + 1 month, same day).

Synthetic dates only — no PDFs, no Drive, no network.
"""
import unittest
from datetime import date

from send_morning_digest import acct_group, is_reconciliation_current


class AcctGroupTest(unittest.TestCase):
    def test_checking_and_savings_are_bank_accounts(self):
        self.assertEqual(acct_group("bofa_checking"), "Bank Accounts")
        self.assertEqual(acct_group("citi_savings"), "Bank Accounts")

    def test_payroll_is_its_own_group(self):
        self.assertEqual(acct_group("payroll"), "Payroll")

    def test_credit_cards_recognized(self):
        for key in ("bofa_credit", "chase_sapphire", "wf_visa",
                    "citi_visa_costco", "bmo_credit_cardholder"):
            self.assertEqual(acct_group(key), "Credit Cards")

    def test_amex_checking_is_bank_account_not_credit_card(self):
        self.assertEqual(acct_group("amex_checking"), "Bank Accounts")


class IsReconciliationCurrentTest(unittest.TestCase):
    """Mid-July: last month's (EOM) statement should still read as current,
    since this month's own statement hasn't closed yet. Mid-month-closing CC
    accounts should be judged against their own next closing date instead."""

    def setUp(self):
        self.today = date(2026, 7, 23)

    def test_none_last_date_is_never_current(self):
        self.assertFalse(is_reconciliation_current(None, "Bank Accounts", self.today))

    def test_eom_account_reconciled_through_prior_month_end_is_current(self):
        # This was the reported bug: a checking/savings account reconciled
        # through 06/30 must not be flagged overdue on 07/23 — July's own
        # statement doesn't close until 07/31.
        last_date = date(2026, 6, 30)
        self.assertTrue(is_reconciliation_current(last_date, "Bank Accounts", self.today))
        self.assertTrue(is_reconciliation_current(last_date, "Payroll", self.today))

    def test_eom_account_two_months_stale_is_overdue(self):
        last_date = date(2026, 5, 31)
        self.assertFalse(is_reconciliation_current(last_date, "Bank Accounts", self.today))

    def test_eom_account_becomes_overdue_once_new_month_ends(self):
        last_date = date(2026, 6, 30)
        after_july_close = date(2026, 8, 1)
        self.assertFalse(is_reconciliation_current(last_date, "Bank Accounts", after_july_close))

    def test_cc_account_current_until_next_closing_day_passes(self):
        last_date = date(2026, 6, 14)  # closes on the 14th each month
        self.assertTrue(is_reconciliation_current(last_date, "Credit Cards", date(2026, 7, 14)))
        self.assertTrue(is_reconciliation_current(last_date, "Credit Cards", date(2026, 7, 15)))
        self.assertFalse(is_reconciliation_current(last_date, "Credit Cards", date(2026, 7, 16)))

    def test_cc_account_stale_past_next_close_is_overdue(self):
        last_date = date(2026, 6, 6)
        self.assertFalse(is_reconciliation_current(last_date, "Credit Cards", self.today))


if __name__ == "__main__":
    unittest.main()
