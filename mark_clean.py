#!/usr/bin/env python3
"""
mark_clean.py
-------------
Upgrade a IN_PROGRESS entry to DONE and trigger the Google Sheets tracker update.

Usage:
    python3 mark_clean.py <client_key> <account_type> [<statement_date>]

    <client_key>     canonical tracker key, e.g. ACME_INC
    <account_type>   e.g. citi_checking, bofa_credit, payroll
    <statement_date> optional — MM/DD/YY or YYYY-MM-DD.  If omitted and only
                     one IN_PROGRESS entry matches client+account, uses that date.

Examples:
    python3 mark_clean.py ACME_INC citi_checking
    python3 mark_clean.py ACME_INC citi_checking 05/28/26
    python3 mark_clean.py ACME_INC bofa_credit 06/06/26
"""

import json
import sys
from pathlib import Path

REPO_DIR   = Path(__file__).parent
import sys as _sys
_sys.path.insert(0, str(REPO_DIR))
from log_utils import get_logs_dir as _get_logs_dir
# Operational logs live in the private logs dir, not the public repo.
LOGS_DIR   = _get_logs_dir()
LOG_PATH   = LOGS_DIR / "recon_log.json"
CSV_PATH   = LOGS_DIR / "reconciliation_log.csv"


def _load_log():
    if LOG_PATH.exists():
        with open(LOG_PATH) as f:
            return json.load(f)
    return []


def _save_log(entries):
    with open(LOG_PATH, "w") as f:
        json.dump(entries, f, indent=2)
        f.write("\n")


def _normalize_date(s: str) -> str:
    """Normalize to MM/DD/YY for display / CSV matching."""
    from datetime import datetime
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%m/%d/%y")
        except ValueError:
            pass
    return s.strip()


def find_pending(client_key: str, account_type: str, statement_date: str | None):
    """
    Find a IN_PROGRESS recon_log entry matching the given criteria.
    Matches on client (case-insensitive key variants) and account_type.
    If statement_date is given, also matches on that.
    Returns (index, entry) or raises SystemExit if not found / ambiguous.
    """
    entries = _load_log()
    from log_utils import _normalize_client_key

    candidates = []
    for i, e in enumerate(entries):
        if e.get("status") != "IN_PROGRESS":
            continue
        if e.get("type") != "recon":
            continue
        ck = _normalize_client_key(e.get("client", ""))
        ck_upper, key_upper = ck.upper(), client_key.upper()
        # Accept the full tracker key (exact match) or the shorter
        # human-friendly key documented in CLAUDE.md's client table, which
        # drops the legal suffix the tracker key keeps (e.g. "acme_salon"
        # vs. tracker key "ACME_SALON_LLC"). Boundary on '_' so a
        # short key can't accidentally prefix-match an unrelated client.
        if ck_upper != key_upper and not ck_upper.startswith(key_upper + '_'):
            continue
        if e.get("account_type", "").lower() != account_type.lower():
            continue
        if statement_date:
            norm = _normalize_date(statement_date)
            entry_date = _normalize_date(e.get("statement_end_date", ""))
            if entry_date != norm:
                continue
        candidates.append((i, e))

    if not candidates:
        print(f"ERROR: No IN_PROGRESS entry found for {client_key} / {account_type}"
              + (f" / {statement_date}" if statement_date else "") + ".")
        print("\nCurrent IN_PROGRESS entries:")
        for e in entries:
            if e.get("status") == "IN_PROGRESS":
                print(f"  {e.get('client')} | {e.get('account_type')} | {e.get('statement_end_date')}")
        sys.exit(1)

    if len(candidates) > 1:
        print(f"ERROR: Multiple IN_PROGRESS entries match {client_key} / {account_type}.")
        print("Specify a statement_date to disambiguate:")
        for _, e in candidates:
            print(f"  {e.get('statement_end_date')}")
        sys.exit(1)

    return candidates[0]


def upgrade_to_clean(idx: int, entry: dict):
    """Rewrite the entry in recon_log.json with status=DONE and a fresh run_time."""
    from log_utils import _now_pst
    entries = _load_log()
    updated = dict(entry)
    updated["status"]   = "DONE"
    updated["run_time"] = _now_pst().isoformat()
    entries[idx] = updated
    _save_log(entries)
    print(f"  ✅ recon_log.json → DONE  ({entry['client']} | {entry['account_type']} | {entry['statement_end_date']})")
    return updated


def update_csv(entry: dict):
    """Upsert the entry into reconciliation_log.csv (statement_date column).

    Matches on (client, account_type, statement_date) — the same three-part
    key write_both_logs() uses — not just (client, account_type). Matching
    without the date used to replace whatever row happened to be first for
    that client+account, silently destroying a *different* statement's row
    (e.g. clobbering a prior month's entry when marking the current month
    clean). `entry` (a recon_log.json row) also has no reliable
    `total_payments` field — the old code substituted `difference` (the
    balance-verification delta, not the payments total), writing wrong
    figures into the tracker. total_payments is now preserved from the
    existing CSV row when one is found, and left blank only when this is a
    genuinely new row with no prior data to preserve.
    """
    import csv
    from log_utils import _normalize_client_key, _normalize_date_iso, _now_pst

    client_key    = _normalize_client_key(entry.get("client", ""))
    account_type  = entry.get("account_type", "")
    stmt_date     = _normalize_date(entry.get("statement_end_date", ""))
    stmt_date_iso = _normalize_date_iso(entry.get("statement_end_date", ""))
    ts            = _now_pst().strftime("%Y-%m-%d %H:%M:%S")

    fields = ["client", "client_name", "account_type", "account_ending",
              "statement_date", "beginning_balance", "ending_balance",
              "total_payments", "run_timestamp", "source"]

    existing = []
    if CSV_PATH.exists():
        with open(CSV_PATH, newline="") as f:
            existing = list(csv.DictReader(f))

    match_idx = None
    for i, r in enumerate(existing):
        if (r.get("client") == client_key
                and r.get("account_type") == account_type
                and _normalize_date_iso(r.get("statement_date", "")) == stmt_date_iso):
            match_idx = i
            break

    row = {
        "client":            client_key,
        "client_name":       entry.get("client", ""),
        "account_type":      account_type,
        "account_ending":    existing[match_idx].get("account_ending", "") if match_idx is not None else "",
        # ISO (YYYY-MM-DD), matching write_both_logs()'s convention for this
        # same CSV column ("for consistent ISO-sortable storage"). Writing
        # stmt_date (MM/DD/YY) here — as opposed to stmt_date_iso, already
        # computed above for the match key — made send_morning_digest.py's
        # naive string "most recent" comparison pick a stale prior date over
        # a real newer one: "2026-06-22" sorts ahead of "07/22/26"
        # lexicographically even though July is chronologically later.
        # Confirmed live against real production data.
        "statement_date":    stmt_date_iso,
        "beginning_balance": entry.get("beginning_balance", ""),
        "ending_balance":    entry.get("ending_balance", ""),
        "total_payments":    existing[match_idx].get("total_payments", "") if match_idx is not None else "",
        "run_timestamp":     ts,
        "source":            "mark_clean",
    }

    if match_idx is not None:
        existing[match_idx] = row
        verb = "Updated"
    else:
        existing.append(row)
        verb = "Appended"

    with open(CSV_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(existing)

    print(f"  📋 {verb} → reconciliation_log.csv  ({stmt_date}  ending ${row['ending_balance']})")


def _ensure_credentials():
    """Load GOOGLE_SHEETS_CREDENTIALS from Bookkeeping-clients if not already set."""
    import os
    if os.environ.get("GOOGLE_SHEETS_CREDENTIALS"):
        return
    clients_dir = os.environ.get("BOOKKEEPING_CLIENTS_DIR") or str(Path.home() / ".bookkeeping" / "clients")
    creds_file  = Path(clients_dir) / "sheets_credentials.json"
    if creds_file.exists():
        os.environ["GOOGLE_SHEETS_CREDENTIALS"] = creds_file.read_text()


def trigger_sheet_update(entry: dict):
    """Push the date to the Google Sheets tracker."""
    _ensure_credentials()
    try:
        from sheets_updater import update_sheet
        from log_utils import _normalize_client_key
    except ImportError as e:
        print(f"  ⚠️  sheets_updater not available: {e} — skipping sheet update")
        return

    client_key   = _normalize_client_key(entry.get("client", ""))
    account_type = entry.get("account_type", "")
    stmt_date    = _normalize_date(entry.get("statement_end_date", ""))

    ok = update_sheet(client_key, account_type, stmt_date)
    if ok:
        print(f"  📊 Google Sheet updated  ({client_key} | {account_type} | {stmt_date})")
    else:
        print(f"  ⚠️  Sheet update failed or cell not mapped for {client_key} / {account_type}")


def git_push():
    try:
        import sys as _sys, os as _os
        _sys.path.insert(0, str(Path(__file__).parent))
        _os.environ.setdefault("BOOKKEEPING_CLIENTS_DIR", str(LOGS_DIR))
        from tools.github_clients import sync_up
        sync_up("mark_clean: upgrade IN_PROGRESS → DONE")
    except Exception as e:
        print(f"  ⚠ Could not push logs via REST API ({e}). Push manually.")


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        print(__doc__)
        sys.exit(1)

    client_key     = args[0]
    account_type   = args[1]
    statement_date = args[2] if len(args) >= 3 else None

    print(f"\nLooking for IN_PROGRESS: {client_key} / {account_type}"
          + (f" / {statement_date}" if statement_date else "") + " ...")

    idx, entry = find_pending(client_key, account_type, statement_date)

    updated = upgrade_to_clean(idx, entry)
    update_csv(updated)
    trigger_sheet_update(updated)
    git_push()

    print("\n✅ Done.")


if __name__ == "__main__":
    main()
