#!/usr/bin/env python3
"""
mark_clean.py
-------------
Upgrade a IN_PROGRESS entry to CLEAN and trigger the Google Sheets tracker update.

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
        if ck.upper() != client_key.upper():
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
    """Rewrite the entry in recon_log.json with status=CLEAN and a fresh run_time."""
    from log_utils import _now_pst
    entries = _load_log()
    updated = dict(entry)
    updated["status"]   = "CLEAN"
    updated["run_time"] = _now_pst().isoformat()
    entries[idx] = updated
    _save_log(entries)
    print(f"  ✅ recon_log.json → CLEAN  ({entry['client']} | {entry['account_type']} | {entry['statement_end_date']})")
    return updated


def update_csv(entry: dict):
    """Upsert the entry into reconciliation_log.csv (statement_date column)."""
    import csv
    from log_utils import _normalize_client_key, _now_pst

    client_key   = _normalize_client_key(entry.get("client", ""))
    account_type = entry.get("account_type", "")
    stmt_date    = _normalize_date(entry.get("statement_end_date", ""))
    ts           = _now_pst().strftime("%Y-%m-%d %H:%M:%S")

    fields = ["client", "client_name", "account_type", "account_ending",
              "statement_date", "beginning_balance", "ending_balance",
              "total_payments", "run_timestamp", "source"]

    row = {
        "client":            client_key,
        "client_name":       entry.get("client", ""),
        "account_type":      account_type,
        "account_ending":    "",
        "statement_date":    stmt_date,
        "beginning_balance": entry.get("beginning_balance", ""),
        "ending_balance":    entry.get("ending_balance", ""),
        "total_payments":    entry.get("difference", ""),
        "run_timestamp":     ts,
        "source":            "mark_clean",
    }

    existing = []
    if CSV_PATH.exists():
        with open(CSV_PATH, newline="") as f:
            existing = list(csv.DictReader(f))

    replaced = False
    for i, r in enumerate(existing):
        if r.get("client") == client_key and r.get("account_type") == account_type:
            existing[i] = row
            replaced = True
            break
    if not replaced:
        existing.append(row)

    with open(CSV_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(existing)

    verb = "Updated" if replaced else "Appended"
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
    import subprocess
    # Logs live in the private logs dir (its own git repo) — commit there.
    cwd = str(LOGS_DIR)
    subprocess.run(
        ["git", "add", "recon_log.json", "reconciliation_log.csv"],
        cwd=cwd, capture_output=True
    )
    result = subprocess.run(
        ["git", "commit", "-m", f"mark_clean: upgrade IN_PROGRESS → CLEAN"],
        cwd=cwd, capture_output=True
    )
    if result.returncode == 0:
        subprocess.run(["git", "push"], cwd=cwd, capture_output=True)
        print("  🚀 Committed and pushed to git")
    else:
        print("  ℹ️  Nothing new to commit to git")


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
