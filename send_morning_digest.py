#!/usr/bin/env python3
"""
send_morning_digest.py
Reads yesterday's reconciliation + manual issue logs and sends a morning
digest email via Gmail SMTP (HTML format).

Required env vars: GMAIL_APP_PASSWORD
Usage: python send_morning_digest.py [--date YYYY-MM-DD]
"""

import json, os, sys, csv, argparse, smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import date, timedelta, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from log_utils import load_private_json, get_logs_dir

# Operational logs (recon_log.json, reconciliation_log.csv) live in the private
# logs dir, not the public repo.
LOG_DIR     = get_logs_dir()
_DIGEST_CFG = load_private_json("digest_config.json", default={})
_EMAIL_CFG  = _DIGEST_CFG.get("email", {})

SENDER       = _EMAIL_CFG.get("sender", "")
RECIPIENT    = _EMAIL_CFG.get("recipient", "")
CC_RECIPIENT = _EMAIL_CFG.get("cc_recipient", "")
SHEET_URL    = _EMAIL_CFG.get("sheet_url", "")

CLIENT_DISPLAY_NAMES = _DIGEST_CFG.get("client_display_names", {})


def display_name(raw: str) -> str:
    """Normalize a raw client name to its preferred display name."""
    return CLIENT_DISPLAY_NAMES.get(raw.strip().lower(), raw.strip())


ACCOUNT_DISPLAY_NAMES = _DIGEST_CFG.get("account_display_names", {})


def display_account(raw: str) -> str:
    """Normalize a raw account_type to its preferred display name."""
    key = raw.strip().lower()
    if key in ACCOUNT_DISPLAY_NAMES:
        return ACCOUNT_DISPLAY_NAMES[key]
    return raw.strip().replace("_", " ").title()

TRACKER = _DIGEST_CFG.get("tracker", [])



def load_log(log_date):
    """Load recon (non-manual) entries for a given date from recon_log.json."""
    from log_utils import load_recon_log
    recon, _ = load_recon_log(log_date)
    return recon


def load_manual_issues(log_date):
    """Load manual issue entries for a given date from recon_log.json."""
    from log_utils import load_recon_log
    _, manual = load_recon_log(log_date)
    return manual


def load_reconciliation_log():
    log_file = LOG_DIR / "reconciliation_log.csv"
    if not log_file.exists():
        return {}
    # Normalize account types so variants map to the TRACKER key.
    # Single source of truth: acct_type_map in sheets_config.json.
    # Do NOT hardcode aliases here — add them to sheets_config.json instead.
    try:
        from log_utils import load_private_json
        _sheets_cfg = load_private_json("sheets_config.json") or {}
        _at_aliases = _sheets_cfg.get("acct_type_map", {})
    except Exception:
        _at_aliases = {}
    latest = {}
    with open(log_file, newline="") as f:
        for row in csv.DictReader(f):
            ck, at, sd = row.get("client","").strip(), row.get("account_type","").strip(), row.get("statement_date","").strip()
            if ck and at and sd:
                at = _at_aliases.get(at, at)
                # Keep the most recent date if duplicate keys
                existing = latest.get((ck, at))
                if not existing or sd > existing:
                    latest[(ck, at)] = sd
    return latest


def parse_date(date_str):
    """Parse a date string into a comparable date object. Returns None if unparseable."""
    from datetime import datetime
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except Exception:
            pass
    return None


def normalize_date(date_str):
    """Normalize any date string to MM/DD/YY format."""
    if not date_str or date_str == "—":
        return date_str
    from datetime import datetime
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str.strip(), fmt).strftime("%m/%d/%y")
        except Exception:
            pass
    return date_str  # return as-is if unparseable


def get_tracker_date(recon_dates, client_name, client_keys, acct):
    """
    Return the most recent reconciled date from reconciliation_log.csv.
    Returns "—" if no entry found.
    """
    for ck in client_keys:
        val = recon_dates.get((ck, acct["key"]))
        if val:
            return normalize_date(val)
    return "—"



CC_BLOCKING_RULES = _DIGEST_CFG.get("cc_blocking_rules", {})


def get_cell_style(client_name, acct_key, recon_dates, client_keys, today=None,
                   client_provided=False):
    """
    Returns (bg_color, text_color, tooltip, label_override) for a tracker date cell.
    label_override is None to show the normal date, or a string to replace it.

    Logic for clients with blocking rules:
    - CC blocker cell:
        - Green if already reconciled for current cycle
        - Yellow if not yet done (upcoming / overdue)
    - Blocked (checking/savings) cell:
        - Green if all CC blockers are done for current cycle
        - Orange with "Waiting on CC" if CC pending but within 1 month
        - Pink with "Blocked — prior step" if CC is more than 1 month overdue
    - All other cells: no highlighting
    """
    from datetime import date as date_cls, timedelta
    if today is None:
        today = date_cls.today()

    def last_reconciled(key):
        for ck in client_keys:
            val = recon_dates.get((ck, key))
            if val:
                d = parse_date(val)
                if d:
                    return d
        fb = None
        for acct in next((c["accounts"] for c in TRACKER if c["client"] == client_name), []):
            if acct["key"] == key:
                fb = "—"
                break
        return parse_date(fb) if fb else None

    # Client-provided accounts — we wait for the client to send the statement
    if client_provided:
        acct_last = last_reconciled(acct_key)
        prev_month_end = today.replace(day=1) - timedelta(days=1)
        if acct_last and acct_last >= prev_month_end.replace(day=1):
            return '#dcfce7', '#166534', '✅ Up to date', None
        else:
            return '#f1f5f9', '#64748b', '📬 Waiting on client', None

    rules = CC_BLOCKING_RULES.get(client_name)
    if not rules:
        return "#f9fafb", "#1f2937", "", None  # no highlight

    def current_closing(closing_day):
        """Return the most recent closing date on or before today."""
        import calendar
        try:
            candidate = today.replace(day=closing_day)
        except ValueError:
            last = calendar.monthrange(today.year, today.month)[1]
            candidate = today.replace(day=min(closing_day, last))
        if candidate > today:
            if today.month == 1:
                candidate = candidate.replace(year=today.year-1, month=12)
            else:
                candidate = candidate.replace(month=today.month-1)
        return candidate

    def next_closing(closing_day):
        """Return the next expected closing date after today."""
        curr = current_closing(closing_day)
        if curr.month == 12:
            return curr.replace(year=curr.year+1, month=1)
        return curr.replace(month=curr.month+1)

    # ── CC blocker cell ──
    if acct_key in [b["key"] for b in rules["cc_blockers"]]:
        blocker    = next(b for b in rules["cc_blockers"] if b["key"] == acct_key)
        last_done  = last_reconciled(acct_key)
        curr_close = current_closing(blocker["closing_day"])
        nxt_close  = next_closing(blocker["closing_day"])
        if nxt_close > today:
            # Next closing hasn't happened yet
            if last_done and last_done >= curr_close:
                # Current cycle reconciled and next statement not due yet —
                # nothing to do, so show it as up to date (not blocked).
                nxt_str = nxt_close.strftime("%m/%d/%y")
                return "#dcfce7", "#166534", f"✅ CC up to date — next due {nxt_str}", None
            else:
                # Current cycle not done and next not due — overdue
                return "#fce7f3", "#9d174d", f"⚠️ Overdue — due {curr_close.strftime('%m/%d/%y')}", None
        else:
            # Next closing has passed
            if last_done and last_done >= nxt_close:
                return "#dcfce7", "#166534", "✅ CC up to date", None
            elif last_done and last_done >= curr_close:
                # Current cycle done but new statement now available
                nxt_str = nxt_close.strftime("%m/%d/%y")
                return "#fef9c3", "#92400e", f"⏳ Statement available — reconcile {nxt_str}", None
            else:
                return "#fce7f3", "#9d174d", f"⚠️ Overdue — due {curr_close.strftime('%m/%d/%y')}", None

    # ── Blocked checking/savings cell ──
    if acct_key in rules["blocked"]:
        # Check if all CC blockers are reconciled through their most recent closed cycle
        all_cc_current = True
        next_cc_due = None
        most_overdue = 0
        for blocker in rules["cc_blockers"]:
            last_done  = last_reconciled(blocker["key"])
            curr_close = current_closing(blocker["closing_day"])
            nxt_close  = next_closing(blocker["closing_day"])
            # CC is current if it's reconciled through curr_close
            if not last_done or last_done < curr_close:
                all_cc_current = False
                if last_done:
                    months = (curr_close.year - last_done.year) * 12 + (curr_close.month - last_done.month)
                else:
                    months = 99
                most_overdue = max(most_overdue, months)
            # Track the soonest upcoming CC close
            if next_cc_due is None or nxt_close < next_cc_due:
                next_cc_due = nxt_close

        if not all_cc_current:
            # CC not done — checking is blocked
            if most_overdue > 1:
                return "#fce7f3", "#9d174d", "🚫 Blocked — CC more than 1 month overdue", "Blocked"
            elif next_cc_due and next_cc_due > today:
                due_str = next_cc_due.strftime("%m/%d/%y")
                return "#fff7ed", "#c2410c", f"🔒 Waiting on CC — due {due_str}", None
            else:
                return "#fff7ed", "#c2410c", "🔒 Waiting on CC reconciliation", None

        # CC is current — check if the CC statement containing last month's payment
        # has closed and been reconciled.
        #
        # Rule (based on observed payment timing):
        #   closing_day <= 15 → payment appears on THIS month's close (early-month close)
        #   closing_day >  15 → payment appears on LAST month's close (late-month close)
        import calendar as _cal
        prev_month_end = today.replace(day=1) - timedelta(days=1)

        not_ready = []
        for b in rules["cc_blockers"]:
            cd = b["closing_day"]
            if cd <= 15:
                # Early-month close: payment shows on this month's statement
                try:
                    gate_date = today.replace(day=cd)
                except ValueError:
                    gate_date = today.replace(day=min(cd, _cal.monthrange(today.year, today.month)[1]))
            else:
                # Late-month close: payment shows on last month's statement
                try:
                    gate_date = prev_month_end.replace(day=cd)
                except ValueError:
                    gate_date = prev_month_end.replace(day=min(cd, _cal.monthrange(prev_month_end.year, prev_month_end.month)[1]))

            if gate_date > today:
                not_ready.append(gate_date)
            else:
                cc_last = last_reconciled(b["key"])
                if not cc_last or cc_last < gate_date:
                    not_ready.append(gate_date)

        if not_ready:
            due_str = min(not_ready).strftime("%m/%d/%y")
            return "#fff7ed", "#c2410c", f"🔒 Waiting on CC — due {due_str}", None

        # Next CC has closed and is reconciled — checking is actionable
        acct_last = last_reconciled(acct_key)
        prev_month_end = today.replace(day=1) - timedelta(days=1)
        prev_month_start = prev_month_end.replace(day=1)
        if acct_last and acct_last >= prev_month_start:
            return "#dcfce7", "#166534", "✅ Up to date", None
        elif acct_last:
            months_behind = (prev_month_end.year - acct_last.year) * 12 + (prev_month_end.month - acct_last.month)
            if months_behind > 1:
                return "#fce7f3", "#9d174d", f"⚠️ {months_behind} months behind — reconcile now", None
            else:
                return "#fef9c3", "#92400e", "⏳ Ready to reconcile", None
        else:
            return "#fef9c3", "#92400e", "⏳ Ready to reconcile", None

    # ── Payroll cell — blocked until checking is done ──
    if acct_key in rules.get("payroll_blocked", []):
        checking_key  = rules.get("checking_key")
        checking_done = last_reconciled(checking_key) if checking_key else None
        prev_month_end   = today.replace(day=1) - timedelta(days=1)
        prev_month_start = prev_month_end.replace(day=1)
        if checking_done and checking_done >= prev_month_start:
            # Checking is current — payroll is actionable
            payroll_last = last_reconciled(acct_key)
            if payroll_last and payroll_last >= prev_month_start:
                return "#dcfce7", "#166534", "✅ Payroll up to date", None
            else:
                return "#fef9c3", "#92400e", "⏳ Ready to reconcile", None
        else:
            return "#fff7ed", "#c2410c", "🔒 Waiting on checking reconciliation", None
        if False:  # keep else branch syntactically valid
            return "#fff7ed", "#c2410c", "🔒 Waiting on checking reconciliation", None

    return "#f9fafb", "#1f2937", "", None

def build_html(recon_entries, manual_entries, log_date):
    dates = sorted(d.strip() for d in log_date.split(",") if d.strip())
    try:
        if len(dates) == 1:
            friendly_date = date.fromisoformat(dates[0]).strftime("%B %-d, %Y")
        else:
            first = date.fromisoformat(dates[0]).strftime("%B %-d")
            last  = date.fromisoformat(dates[-1]).strftime("%-d, %Y")
            friendly_date = f"{first}–{last}"
    except Exception:
        friendly_date = log_date

    has_manual_issues = len(manual_entries) > 0
    has_any_issues    = has_manual_issues

    subject = f"Reconciliation Digest — {friendly_date}"

    status_color = "#b45309" if has_any_issues else "#166534"
    status_bg    = "#fef9c3" if has_any_issues else "#dcfce7"
    status_text  = "⚠️ Issues Require Attention" if has_any_issues else "✅ All Clear"

    recon_dates = load_reconciliation_log()

    # ── Build tracker HTML ──
    tracker_rows = ""
    for client in TRACKER:
        name     = client["client"]
        accounts = client["accounts"]
        keys     = client["client_keys"]
        header_cells = "".join(
            f'<th style="padding:6px 12px;text-align:center;font-weight:600;font-size:12px;color:#374151;border:1px solid #e5e7eb">{a["label"]}</th>'
            for a in accounts
        )
        def make_date_cell(a):
            d     = get_tracker_date(recon_dates, name, keys, a)
            bg, fg, tip, label_override = get_cell_style(name, a["key"], recon_dates, keys,
                                                              client_provided=a.get("client_provided", False))
            display = label_override if label_override else d
            title = f' title="{tip}"' if tip else ""
            return (f'<td{title} style="padding:6px 12px;text-align:center;font-size:13px;'
                    f'color:{fg};background:{bg};border:1px solid #e5e7eb;font-weight:500">{display}</td>')
        date_cells = "".join(make_date_cell(a) for a in accounts)
        tracker_rows += f"""
        <tr><td colspan="{len(accounts)}" style="padding:8px 12px 2px;font-weight:700;font-size:13px;color:#1e40af;background:#eff6ff;border:1px solid #e5e7eb">{name}</td></tr>
        <tr>{header_cells}</tr>
        <tr style="background:#f9fafb">{date_cells}</tr>
        <tr><td colspan="{len(accounts)}" style="padding:4px;border:none"></td></tr>
        """

    # ── Build manual notes HTML ──
    manual_html = ""
    if manual_entries:
        # Group by client
        grouped = {}
        for e in manual_entries:
            client = display_name(e.get("client", "General"))
            grouped.setdefault(client, []).append(e)

        client_blocks = ""
        for client, entries in grouped.items():
            items = "".join(
                f'<li style="padding:4px 0;border-bottom:1px solid #fde68a;font-size:13px;color:#374151">'
                f'{e.get("issue","")}'
                f'<span style="color:#9ca3af;font-size:11px;margin-left:8px">({e.get("run_time","")[:16].replace("T"," ")})</span>'
                f'</li>'
                for e in entries
            )
            client_blocks += f"""
            <div style="margin-bottom:12px">
              <div style="font-weight:700;font-size:13px;color:#92400e;margin-bottom:4px">{client}</div>
              <ul style="margin:0;padding:0 0 0 16px;list-style:disc">
                {items}
              </ul>
            </div>
            """
        manual_html = f"""
        <div style="margin-bottom:24px;background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:14px 16px">
          <div style="font-weight:700;font-size:14px;color:#92400e;margin-bottom:10px">⚠️ Manual Notes</div>
          {client_blocks}
        </div>
        """

    # ── Group all entries by (display_client, account_type) — one card per group ──
    # Deduplicate first by (client, account_type, statement_end_date) — keep latest run_time
    seen = {}
    for e in recon_entries:
        key = (e.get("client",""), e.get("account_type",""), e.get("statement_end_date",""))
        if key not in seen or e.get("run_time","") > seen[key].get("run_time",""):
            seen[key] = e

    # Group deduplicated entries by display_client, then by account_type — one card per client
    def _status_style(has_pending):
        if has_pending:
            return ("#7c3aed", "#f5f3ff", "📋 PENDING QB")
        return ("#166534", "#f0fdf4", "✅ DONE")

    clients = {}  # display_client -> {"accounts": {account_type: {...}}, "has_pending"}
    for e in seen.values():
        client_disp  = display_name(e.get("client", "—"))
        account_type = display_account(e.get("account_type", ""))
        c = clients.setdefault(client_disp, {"accounts": {}, "has_pending": False})
        a = c["accounts"].setdefault(account_type, {"dates": [], "has_pending": False})
        d = e.get("statement_end_date", "")
        if d and d not in a["dates"]:
            a["dates"].append(d)
        if e.get("status") == "IN_PROGRESS":
            a["has_pending"] = c["has_pending"] = True

    # ── Build reconciliation runs HTML — one card per client, all account types inside ──
    runs_html = ""
    if clients:
        for client_disp, c in clients.items():
            run_color, run_bg, run_badge = _status_style(c["has_pending"])
            acct_rows = ""
            for account_type, a in c["accounts"].items():
                a_color, _a_bg, a_badge = _status_style(a["has_pending"])
                dates_label = "Completed dates:" if account_type == "Payroll" else "Statement dates:"
                dates_html  = "".join(
                    f'<li style="padding:1px 0;color:#374151">{d}</li>' for d in sorted(a["dates"])
                )
                acct_rows += f"""
                <div style="padding:8px 0;border-top:1px solid #f3f4f6">
                  <div style="display:flex;justify-content:space-between;align-items:center">
                    <span style="font-weight:600;font-size:13px;color:#374151">{account_type}</span>
                    <span style="font-size:12px;font-weight:600;color:{a_color}">{a_badge}</span>
                  </div>
                  <div style="color:#6b7280;margin-top:4px;font-size:12px">{dates_label}</div>
                  <ul style="margin:2px 0 0 16px;padding:0;list-style:disc">{dates_html}</ul>
                </div>"""
            runs_html += f"""
            <div style="border:1px solid #e5e7eb;border-radius:8px;margin-bottom:12px;overflow:hidden">
              <div style="background:{run_bg};padding:10px 14px;display:flex;justify-content:space-between;align-items:center">
                <span style="font-weight:700;font-size:14px;color:#1f2937">{client_disp}</span>
                <span style="font-size:12px;font-weight:600;color:{run_color}">{run_badge}</span>
              </div>
              <div style="padding:2px 14px 10px 14px;font-size:13px;color:#374151">
                {acct_rows}
              </div>
            </div>
            """
    else:
        runs_html = '<p style="color:#6b7280;font-size:13px">No reconciliations ran yesterday.</p>'



    html = f"""
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
<div style="max-width:680px;margin:24px auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,0.08)">

  <!-- Header -->
  <div style="background:#1e3a5f;padding:20px 24px">
    <div style="font-size:18px;font-weight:700;color:#fff">Reconciliation Morning Digest</div>
    <div style="font-size:13px;color:#93c5fd;margin-top:2px">{friendly_date}</div>
  </div>

  <div style="padding:20px 24px">

    <!-- Manual notes -->
    {manual_html}

    <!-- Reconciliation runs -->
    <div style="font-weight:700;font-size:14px;color:#1f2937;margin-bottom:10px">
      Reconciliation Runs — {len(clients)} client(s)
    </div>
    {runs_html}



    <!-- Tracker -->
    <div style="margin-top:28px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
        <span style="font-weight:700;font-size:14px;color:#1f2937">Reconciliation Tracker</span>
        <a href="{SHEET_URL}" style="font-size:12px;color:#2563eb;text-decoration:none">📊 Open in Google Sheets →</a>
      </div>
      <div style="overflow-x:auto">
        <table style="border-collapse:collapse;width:100%;font-size:13px">
          {tracker_rows}
        </table>
      </div>
      <!-- Legend -->
      <div style="margin-top:12px;display:flex;gap:16px;flex-wrap:wrap;font-size:11px;color:#6b7280">
        <span><span style="display:inline-block;width:12px;height:12px;background:#dcfce7;border:1px solid #bbf7d0;border-radius:2px;vertical-align:middle;margin-right:4px"></span>Ready / up to date</span>
        <span><span style="display:inline-block;width:12px;height:12px;background:#fef9c3;border:1px solid #fde68a;border-radius:2px;vertical-align:middle;margin-right:4px"></span>CC due — not yet reconciled</span>
        <span><span style="display:inline-block;width:12px;height:12px;background:#fff7ed;border:1px solid #fed7aa;border-radius:2px;vertical-align:middle;margin-right:4px"></span>Blocked — waiting on prior step</span>
        <span><span style="display:inline-block;width:12px;height:12px;background:#fce7f3;border:1px solid #fbcfe8;border-radius:2px;vertical-align:middle;margin-right:4px"></span>Blocked — CC more than 1 month overdue</span>
        <span><span style="display:inline-block;width:12px;height:12px;background:#f9fafb;border:1px solid #e5e7eb;border-radius:2px;vertical-align:middle;margin-right:4px"></span>No rule</span>
      </div>
    </div>

  </div>

  <!-- Footer -->
  <div style="background:#f9fafb;padding:12px 24px;font-size:11px;color:#9ca3af;border-top:1px solid #e5e7eb;text-align:center">
    Sent automatically by Bookkeeping Digest · sarah-levine/Bookkeeping
  </div>

</div>
</body>
</html>
"""
    return subject, html


def send_via_smtp(subject: str, html: str, include_cc: bool = False) -> bool:
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    if not app_password:
        print("ERROR: GMAIL_APP_PASSWORD not set.", file=sys.stderr)
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SENDER
    msg["To"]      = RECIPIENT
    recipients     = [RECIPIENT]
    if include_cc:
        msg["Cc"] = CC_RECIPIENT
        recipients.append(CC_RECIPIENT)
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SENDER, app_password.replace(" ", ""))
            server.sendmail(SENDER, recipients, msg.as_string())
        return True
    except smtplib.SMTPAuthenticationError:
        print("ERROR: Gmail auth failed. Check GMAIL_APP_PASSWORD.", file=sys.stderr)
        return False
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return False


def sync_sheet_to_log():
    log_file = LOG_DIR / "reconciliation_log.csv"
    fieldnames = ["client","client_name","account_type","account_ending",
                  "statement_date","beginning_balance","ending_balance",
                  "total_payments","run_timestamp","source"]
    existing, rows = set(), []
    if log_file.exists():
        with open(log_file, newline="") as f:
            for row in csv.DictReader(f):
                rows.append(row)
                existing.add((row.get("client",""), row.get("account_type","")))
    added = 0
    for client in TRACKER:
        ck = client["client_keys"][0]
        for acct in client["accounts"]:
            sd = "—"
            # Check ALL client keys for this client — not just the first
            already_exists = any((k, acct["key"]) in existing for k in client["client_keys"])
            if sd and not already_exists:
                rows.append({"client":ck,"client_name":client["client"],"account_type":acct["key"],
                             "account_ending":"","statement_date":sd,"beginning_balance":"",
                             "ending_balance":"","total_payments":"","run_timestamp":"manual","source":"sheet"})
                existing.add((ck, acct["key"]))
                added += 1
    if added > 0:
        with open(log_file, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        print(f"✅ Synced {added} sheet date(s) into reconciliation_log.csv")


def get_cc_due_today(today=None):
    """
    Returns a list of dicts for every CC blocker whose closing_day matches today.
    Each dict: {client, cc_key, cc_label, closing_day, ready_accounts}
    ready_accounts = list of account labels that are now unblocked (checking/savings/payroll).
    """
    from datetime import date as date_cls
    if today is None:
        today = date_cls.today()

    due = []
    recon_dates = load_reconciliation_log()

    for client_name, rules in CC_BLOCKING_RULES.items():
        keys = next((c["client_keys"] for c in TRACKER if c["client"] == client_name), [])
        accounts = next((c["accounts"] for c in TRACKER if c["client"] == client_name), [])

        for blocker in rules["cc_blockers"]:
            # Trigger the day AFTER the closing day (statement is available next day)
            from datetime import timedelta
            closing_date = today.replace(day=blocker["closing_day"])
            day_after_closing = closing_date + timedelta(days=1)
            if today != day_after_closing:
                continue

            # Find the display label for this CC
            cc_label = next(
                (a["label"] for a in accounts if a["key"] == blocker["key"]),
                blocker["key"]
            )

            # List the accounts that will be unblocked once this CC is reconciled
            ready = []
            for acct_key in rules.get("blocked", []):
                label = next((a["label"] for a in accounts if a["key"] == acct_key), acct_key)
                ready.append(label)

            due.append({
                "client":        client_name,
                "cc_key":        blocker["key"],
                "cc_label":      cc_label,
                "closing_day":   blocker["closing_day"],
                "ready_accounts": ready,
            })

    return due


def build_cc_due_email(due_items, today=None):
    """Build subject + HTML for the CC-due action items email."""
    from datetime import date as date_cls
    if today is None:
        today = date_cls.today()

    friendly_date = today.strftime("%B %-d, %Y")
    subject = f"📋 CC Statements Due Today — {friendly_date}"

    # Group by client
    by_client = {}
    for item in due_items:
        by_client.setdefault(item["client"], []).append(item)

    client_blocks = ""
    for client, items in by_client.items():
        rows = ""
        for item in items:
            ready_list = "".join(
                f'<li style="padding:2px 0;font-size:13px;color:#374151">🔓 {acct}</li>'
                for acct in item["ready_accounts"]
            )
            rows += f"""
            <div style="padding:10px 0;border-top:1px solid #e5e7eb">
              <div style="font-weight:600;font-size:13px;color:#1e40af">
                {item['cc_label']} — closes today (day {item['closing_day']})
              </div>
              <div style="margin-top:6px;font-size:12px;color:#6b7280">
                Once reconciled, these accounts are ready:
              </div>
              <ul style="margin:4px 0 0 16px;padding:0;list-style:disc">
                {ready_list}
              </ul>
            </div>"""

        client_blocks += f"""
        <div style="border:1px solid #e5e7eb;border-radius:8px;margin-bottom:12px;overflow:hidden">
          <div style="background:#eff6ff;padding:10px 14px;font-weight:700;font-size:14px;color:#1e40af">
            {client}
          </div>
          <div style="padding:2px 14px 10px 14px">
            {rows}
          </div>
        </div>"""

    html = f"""
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
<div style="max-width:680px;margin:24px auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,0.08)">

  <div style="background:#1e3a5f;padding:20px 24px">
    <div style="font-size:18px;font-weight:700;color:#fff">📋 CC Statements Due Today</div>
    <div style="font-size:13px;color:#93c5fd;margin-top:2px">{friendly_date}</div>
  </div>

  <div style="padding:20px 24px">
    <p style="font-size:13px;color:#374151;margin:0 0 16px">
      The following credit card statements close today.
      Reconcile these first — the accounts listed below will be ready once each CC is done.
    </p>
    {client_blocks}
  </div>

  <div style="background:#f9fafb;padding:12px 24px;font-size:11px;color:#9ca3af;border-top:1px solid #e5e7eb;text-align:center">
    Sent automatically by Bookkeeping Digest · sarah-levine/Bookkeeping
  </div>

</div>
</body>
</html>
"""
    return subject, html


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Log date YYYY-MM-DD. Defaults to yesterday.")
    parser.add_argument("--scheduled", action="store_true",
                        help="Pass this flag when run by the 8am cron schedule. "
                             "Only scheduled runs CC the configured cc_recipient.")
    parser.add_argument("--cc-due", action="store_true",
                        help="Check if any CC statements close today and send action items email if so.")
    args = parser.parse_args()

    # ── CC-due trigger ──
    if args.cc_due:
        from datetime import date as date_cls
        today = date_cls.today()
        due_items = get_cc_due_today(today)
        if not due_items:
            print(f"No CC statements due today ({today}). Nothing to send.")
            sys.exit(0)
        subject, html = build_cc_due_email(due_items, today)
        print(f"Subject: {subject}")
        print(f"CC statements due: {[i['cc_label'] for i in due_items]}")
        success = send_via_smtp(subject, html, include_cc=args.scheduled)
        if success:
            print(f"✅ CC-due email sent for {today}.")
            sys.exit(0)
        else:
            print("❌ Failed to send.", file=sys.stderr)
            sys.exit(1)

    log_date = args.date or (datetime.now(ZoneInfo('America/Los_Angeles')).date() - timedelta(days=1)).isoformat()
    log_dates = [d.strip() for d in log_date.split(",") if d.strip()]
    include_cc = args.scheduled
    print(f"Loading logs for: {', '.join(log_dates)}")
    print(f"CC to {CC_RECIPIENT}: {'YES (scheduled run)' if include_cc else 'NO (manual run)'}")

    sync_sheet_to_log()
    recon_entries  = load_log(log_dates)
    manual_entries = load_manual_issues(log_dates)

    if not recon_entries and not manual_entries:
        print("No entries found — nothing to send.")
        sys.exit(0)

    subject, html = build_html(recon_entries, manual_entries, log_date)

    print(f"Subject: {subject}")
    to_line = f"{RECIPIENT} + CC {CC_RECIPIENT}" if include_cc else RECIPIENT
    print(f"Sending HTML email to {to_line} via Gmail SMTP...")

    success = send_via_smtp(subject, html, include_cc=include_cc)
    if success:
        print(f"✅ Digest sent for {log_date}.")
        sys.exit(0)
    else:
        print("❌ Failed to send.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
