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
import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path

import send_morning_digest as smd
from send_morning_digest import (
    acct_group, is_reconciliation_current, build_digest_email,
    compute_overdue_accounts, has_newly_overdue_accounts,
    filter_new_manual_entries, should_send_digest,
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


class ShouldSendDigestTest(unittest.TestCase):
    """The whole send decision, in one place, exercised by one test —
    covers the exact scenario that slipped through in #34: the overdue
    trigger was correctly fixed to stop re-firing, but the digest kept
    sending daily anyway because a sibling trigger (stale unresolved
    manual notes) had the same "still true, not actually new" bug and
    nothing tested the combined decision. All five inputs here are
    already-filtered "is this new" booleans/lists (filter_new_manual_
    entries, has_newly_overdue_accounts, etc. do that filtering) —
    should_send_digest's only job is combining them, so these tests use
    plain synthetic values rather than real TRACKER data."""

    def test_everything_stale_stays_silent(self):
        # Old overdue accounts still overdue, old manual notes still
        # unresolved, no new recon activity, no CC due today — exactly
        # what reconciliation_log.csv/recon_log.json looked like on
        # 07/25-07/27 while the digest kept sending anyway.
        should_send, reasons = should_send_digest(
            recon_entries=[], new_manual_entries=[], accounts_due_yesterday=False,
            due_items=[], newly_overdue=False,
        )
        self.assertFalse(should_send)
        self.assertEqual(reasons, [])

    def test_recon_activity_alone_triggers_a_send(self):
        should_send, reasons = should_send_digest(
            recon_entries=[{"client": "X"}], new_manual_entries=[], accounts_due_yesterday=False,
            due_items=[], newly_overdue=False,
        )
        self.assertTrue(should_send)
        self.assertEqual(len(reasons), 1)

    def test_new_manual_note_alone_triggers_a_send(self):
        should_send, reasons = should_send_digest(
            recon_entries=[], new_manual_entries=[{"issue": "x"}], accounts_due_yesterday=False,
            due_items=[], newly_overdue=False,
        )
        self.assertTrue(should_send)
        self.assertEqual(len(reasons), 1)

    def test_account_due_yesterday_alone_triggers_a_send(self):
        should_send, reasons = should_send_digest(
            recon_entries=[], new_manual_entries=[], accounts_due_yesterday=True,
            due_items=[], newly_overdue=False,
        )
        self.assertTrue(should_send)
        self.assertEqual(len(reasons), 1)

    def test_cc_due_today_alone_triggers_a_send(self):
        should_send, reasons = should_send_digest(
            recon_entries=[], new_manual_entries=[], accounts_due_yesterday=False,
            due_items=[{"cc_label": "x"}], newly_overdue=False,
        )
        self.assertTrue(should_send)
        self.assertEqual(len(reasons), 1)

    def test_newly_overdue_alone_triggers_a_send(self):
        should_send, reasons = should_send_digest(
            recon_entries=[], new_manual_entries=[], accounts_due_yesterday=False,
            due_items=[], newly_overdue=True,
        )
        self.assertTrue(should_send)
        self.assertEqual(len(reasons), 1)

    def test_multiple_simultaneous_triggers_all_named_in_reasons(self):
        should_send, reasons = should_send_digest(
            recon_entries=[{"client": "X"}], new_manual_entries=[{"issue": "x"}],
            accounts_due_yesterday=False, due_items=[{"cc_label": "x"}], newly_overdue=True,
        )
        self.assertTrue(should_send)
        self.assertEqual(len(reasons), 4)


class LoadReconciliationLogMixedDateFormatTest(unittest.TestCase):
    """load_reconciliation_log()'s CSV loader picks the most-recent
    statement_date per (client, account_type). Real production data has
    had rows for the same key written in different formats (MM/DD/YY vs
    YYYY-MM-DD, from mark_clean.py vs write_both_logs() disagreeing before
    that was fixed) — a naive string comparison sorts "2026-06-22" ahead
    of "07/22/26" ('2' > '0' lexicographically) even though July is
    chronologically later, silently hiding a newer reconciliation behind
    a stale one. Uses a temp CSV file — no real client data."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.csv_path = Path(self.tmpdir.name) / "reconciliation_log.csv"
        self._orig_log_dir = smd.LOG_DIR
        smd.LOG_DIR = Path(self.tmpdir.name)

    def tearDown(self):
        smd.LOG_DIR = self._orig_log_dir
        self.tmpdir.cleanup()

    def _write_csv(self, rows):
        fields = ["client", "client_name", "account_type", "account_ending",
                  "statement_date", "beginning_balance", "ending_balance",
                  "total_payments", "run_timestamp", "source"]
        with open(self.csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for r in rows:
                writer.writerow(r)

    def _row(self, statement_date):
        return {"client": "TEST_CLIENT_XYZ", "client_name": "Test Client Xyz",
                "account_type": "chase_ink", "account_ending": "",
                "statement_date": statement_date, "beginning_balance": "1",
                "ending_balance": "2", "total_payments": "0",
                "run_timestamp": "2026-07-01 00:00:00", "source": "test"}

    def test_iso_row_does_not_hide_a_later_mm_dd_yy_row(self):
        # Exactly the real scenario: ISO-formatted mid-month row followed
        # by a chronologically later MM/DD/YY row for the same key.
        self._write_csv([
            self._row("05/22/26"),
            self._row("2026-06-22"),
            self._row("07/22/26"),
        ])
        recon_dates = smd.load_reconciliation_log()
        self.assertEqual(recon_dates[("TEST_CLIENT_XYZ", "chase_ink")], "07/22/26")

    def test_all_same_format_still_picks_latest(self):
        self._write_csv([self._row("05/22/26"), self._row("07/22/26"), self._row("06/22/26")])
        recon_dates = smd.load_reconciliation_log()
        self.assertEqual(recon_dates[("TEST_CLIENT_XYZ", "chase_ink")], "07/22/26")

    def test_unparseable_existing_gets_replaced_by_any_parseable_row(self):
        self._write_csv([self._row("not-a-date"), self._row("07/22/26")])
        recon_dates = smd.load_reconciliation_log()
        self.assertEqual(recon_dates[("TEST_CLIENT_XYZ", "chase_ink")], "07/22/26")


if __name__ == "__main__":
    unittest.main()
