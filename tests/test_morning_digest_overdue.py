"""
test_morning_digest_overdue.py
-------------------------------
Regression coverage for send_morning_digest.py's "current vs. overdue"
classification (acct_group / is_reconciliation_current), shared by the
tracker-card badges and the "Overdue — Not Yet Reconciled" email section,
plus the combined-email builder (build_digest_email) that replaced the old
separate CC-due-today email.

Bug this guards against: the overdue section used to compare a reconciled
statement's (year, month) directly against today's (year, month), with no
regard for account closing-cycle shape. Checking/savings accounts close on
the last day of the month (EOM) — reconciled through last month's close, they
were flagged "overdue" for the new month the instant the calendar month
ticked over, days or weeks before that new month's statement had even
closed. Credit cards close mid-month on a fixed day and need the opposite
check (last_date + 1 month, same day).

Also covers the CC-due-today and reconciliation-digest emails being merged
into one send with one subject line, instead of two separate script
invocations each sending its own email.

Synthetic dates only — no PDFs, no Drive, no network.
"""
import unittest
from datetime import date

from send_morning_digest import (
    acct_group, is_reconciliation_current, build_digest_email,
    compute_overdue_accounts, has_newly_overdue_accounts,
    filter_new_manual_entries,
)


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


def _needs_attention_section(html):
    """Slice out just the "Needs Attention" section's HTML. build_digest_email
    always renders the full Reconciliation Tracker grid from the real,
    live reconciliation_log.csv/TRACKER too (unrelated to the synthetic
    due_items/overdue_by_client under test), so assertions must not scan
    the whole html string — a real overdue/CC-current badge elsewhere in
    the live tracker grid would make an assertion pass or fail for the
    wrong reason. Returns "" if the section didn't render (self-closing
    HTML comments bound it on both sides regardless of content)."""
    start = html.find("<!-- Needs attention:")
    end = html.find("<!-- Manual notes -->")
    return html[start:end] if start != -1 and end != -1 else ""


class BuildDigestEmailCombinedTest(unittest.TestCase):
    """The old separate --cc-due invocation is gone — one script run now
    always computes both trigger conditions and builds one email. Uses a
    real TRACKER client name — the merged "Needs Attention" section only
    renders rows for clients build_digest_email finds in TRACKER (since
    get_cc_due_today()/compute_overdue_accounts() are themselves always
    built by iterating TRACKER, a synthetic/non-tracked client name would
    silently produce no rows, which caught a bug in this exact test file
    while implementing the merge)."""

    def setUp(self):
        from send_morning_digest import TRACKER
        self.client = TRACKER[0]["client"]
        self.today = date(2026, 7, 23)
        self.due_items = [{
            "client": self.client, "cc_key": "bofa_credit",
            "cc_label": "BofA Credit Card", "closing_day": 23, "last_date": "06/23/26",
            "ready_accounts": ["BofA Checking", "BofA Savings"],
        }]
        self.overdue = {self.client: [{"label": "Zzz Test Card", "last_date": "06/06/26"}]}

    def test_both_action_items_present_combines_subject_and_section(self):
        subject, html = build_digest_email([], [], "2026-07-22", self.due_items,
                                            self.overdue, self.today)
        section = _needs_attention_section(html)
        self.assertIn("CC Due Today", subject)
        self.assertIn("Past Due", subject)
        self.assertIn("📅 Closes Today", section)
        self.assertIn("🔴 Overdue", section)
        self.assertIn("BofA Credit Card", section)
        self.assertIn("Zzz Test Card", section)

    def test_same_account_overdue_and_closing_today_gets_both_badges_one_row(self):
        overdue = {self.client: [{"label": "BofA Credit Card", "last_date": "06/23/26"}]}
        subject, html = build_digest_email([], [], "2026-07-22", self.due_items,
                                            overdue, self.today)
        section = _needs_attention_section(html)
        # One row for BofA Credit Card carrying both badges, not two rows.
        self.assertEqual(section.count("BofA Credit Card"), 1)
        self.assertIn("📅 Closes Today", section)
        self.assertIn("🔴 Overdue", section)

    def test_cc_due_shows_unlocks_note(self):
        subject, html = build_digest_email([], [], "2026-07-22", self.due_items, {}, self.today)
        self.assertIn("unlocks BofA Checking, BofA Savings", _needs_attention_section(html))

    def test_only_cc_due_omits_overdue_badge(self):
        subject, html = build_digest_email([], [], "2026-07-22", self.due_items, {}, self.today)
        section = _needs_attention_section(html)
        self.assertIn("CC Due Today", subject)
        self.assertNotIn("Past Due", subject)
        self.assertIn("📅 Closes Today", section)
        self.assertNotIn("🔴 Overdue", section)

    def test_only_overdue_omits_cc_due_badge(self):
        subject, html = build_digest_email([], [], "2026-07-22", [], self.overdue, self.today)
        section = _needs_attention_section(html)
        self.assertIn("Past Due", subject)
        self.assertNotIn("CC Due Today", subject)
        self.assertIn("🔴 Overdue", section)
        self.assertNotIn("📅 Closes Today", section)

    def test_no_action_items_falls_back_to_plain_digest_subject(self):
        subject, html = build_digest_email([], [], "2026-07-22", [], {}, self.today)
        self.assertEqual(subject, "Reconciliation Digest — July 22, 2026")
        self.assertNotIn("🔴 Needs Attention", _needs_attention_section(html))


class ManualNotesInlineInTrackerTest(unittest.TestCase):
    """Manual notes render inline inside their client's tracker card instead
    of a separate standalone section — issue entries never carry a real
    account_type, so a note can only be placed at the client level, not on
    a specific account row. A note whose client doesn't match any tracker
    card (a display_name gap, or the "General" fallback) must still be
    visible somewhere rather than silently dropped."""

    def setUp(self):
        from send_morning_digest import TRACKER
        self.client = TRACKER[0]["client"]
        self.today = date(2026, 7, 23)

    def test_note_renders_inside_its_clients_tracker_card(self):
        manual_entries = [{"client": self.client, "issue": "Zzz test issue",
                            "run_time": "2026-07-22T08:00:00"}]
        subject, html = build_digest_email([], manual_entries, "2026-07-22", [], {}, self.today)
        card_start = html.find(f">{self.client}<")
        next_card = html.find('background:#1e3a5f', card_start + 1)
        card_html = html[card_start:next_card if next_card != -1 else len(html)]
        self.assertIn("Zzz test issue", card_html)

    def test_no_standalone_manual_notes_section(self):
        manual_entries = [{"client": self.client, "issue": "Zzz test issue",
                            "run_time": "2026-07-22T08:00:00"}]
        subject, html = build_digest_email([], manual_entries, "2026-07-22", [], {}, self.today)
        self.assertNotIn("⚠️ Manual Notes</div>", html)

    def test_note_for_untracked_client_falls_back_to_other_notes_block(self):
        manual_entries = [{"client": "Not A Real Tracked Client", "issue": "Zzz stray issue",
                            "run_time": "2026-07-22T08:00:00"}]
        subject, html = build_digest_email([], manual_entries, "2026-07-22", [], {}, self.today)
        self.assertIn("⚠️ Other Notes", html)
        self.assertIn("Zzz stray issue", html)


class HasNewlyOverdueAccountsTest(unittest.TestCase):
    """An account overdue for 10 days straight shouldn't re-trigger a send
    every one of those 10 days — only the day it actually crosses over.
    Uses a real TRACKER client/account_key so is_reconciliation_current's
    real closing-cycle math drives the transition, not a synthetic stand-in."""

    def setUp(self):
        from send_morning_digest import TRACKER
        client = next(c for c in TRACKER
                      if any(a["key"] == "bofa_credit" for a in c["accounts"]))
        self.client_key = client["client_keys"][0]
        # bofa_credit closes on the 6th each month (from last_date's day) —
        # current through 07/07, overdue starting 07/08.
        self.recon_dates = {(self.client_key, "bofa_credit"): "06/06/2026"}

    def test_first_overdue_day_is_newly_overdue(self):
        overdue = compute_overdue_accounts(self.recon_dates, date(2026, 7, 8))
        self.assertTrue(has_newly_overdue_accounts(overdue, self.recon_dates, date(2026, 7, 8)))

    def test_second_overdue_day_is_not_newly_overdue(self):
        overdue = compute_overdue_accounts(self.recon_dates, date(2026, 7, 9))
        self.assertFalse(has_newly_overdue_accounts(overdue, self.recon_dates, date(2026, 7, 9)))

    def test_last_current_day_has_no_newly_overdue(self):
        overdue = compute_overdue_accounts(self.recon_dates, date(2026, 7, 7))
        self.assertFalse(has_newly_overdue_accounts(overdue, self.recon_dates, date(2026, 7, 7)))


class FilterNewManualEntriesTest(unittest.TestCase):
    """load_recon_log() intentionally returns every unresolved manual note
    regardless of date (mcp_server.py's open_issues tool needs that) — a
    note left unresolved for a week would otherwise look "new" every one
    of those days and re-trigger a send purely because it's still sitting
    there. Only run_time falling on one of the target log_dates should
    count as new for the send decision."""

    def test_entry_from_target_date_is_new(self):
        entries = [{"client": "X", "issue": "zzz", "run_time": "2026-07-26T09:00:00-07:00"}]
        self.assertEqual(filter_new_manual_entries(entries, ["2026-07-26"]), entries)

    def test_stale_unresolved_entry_from_prior_day_is_not_new(self):
        entries = [{"client": "X", "issue": "zzz", "run_time": "2026-07-13T13:51:00-07:00"}]
        self.assertEqual(filter_new_manual_entries(entries, ["2026-07-26"]), [])

    def test_mixed_batch_keeps_only_matching_dates(self):
        old = {"client": "X", "issue": "old", "run_time": "2026-07-13T13:51:00-07:00"}
        new = {"client": "X", "issue": "new", "run_time": "2026-07-26T09:00:00-07:00"}
        self.assertEqual(filter_new_manual_entries([old, new], ["2026-07-26"]), [new])

    def test_matches_any_of_multiple_log_dates(self):
        entries = [{"client": "X", "issue": "zzz", "run_time": "2026-07-25T09:00:00-07:00"}]
        result = filter_new_manual_entries(entries, ["2026-07-24", "2026-07-25"])
        self.assertEqual(result, entries)


if __name__ == "__main__":
    unittest.main()
