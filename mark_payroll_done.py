#!/usr/bin/env python3
"""
mark_payroll_done.py
---------------------
Log a payroll run that was entered into QuickBooks outside a normal session
(or whose log write was missed at the time) — without re-parsing the ADP
PDFs. Writes payroll_log.csv, reconciliation_log.csv, and recon_log.json
exactly like a normal "done" run would, from just the check date and known
bank credit amount.

Usage:
    python3 mark_payroll_done.py <client_key> <check_date> <bank_credit>

    <client_key>   payroll_key (e.g. acme_inc) or client name/canonical
                   key/alias (e.g. "Acme Inc")
    <check_date>   MM/DD/YY, MM/DD/YYYY, or YYYY-MM-DD
    <bank_credit>  total payroll bank credit amount, e.g. 18234.12

Examples:
    python3 mark_payroll_done.py acme_inc 06/05/26 18234.12
    python3 mark_payroll_done.py "Acme Inc" 06/05/26 9450.00
"""

import sys
from pathlib import Path

REPO_DIR = Path(__file__).parent
sys.path.insert(0, str(REPO_DIR))

from payroll_clients.base import (  # noqa: E402
    _upsert_csv, _git_push_logs,
    PAYROLL_LOG_PATH, PAYROLL_LOG_FIELDS,
    RECON_LOG_PATH, RECON_LOG_FIELDS,
)
from log_utils import (  # noqa: E402
    _assert_known_client, _normalize_client_key, upsert_recon_log, _now_pst,
)


def _normalize_date(s: str) -> str:
    """Normalize to MM/DD/YY for display / CSV matching."""
    from datetime import datetime
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%m/%d/%y")
        except ValueError:
            pass
    return s.strip()


def _find_client(client_key: str):
    """Resolve client_key to (dispatch_key, client_name).

    dispatch_key is whatever the normal ADP scripts would pass as `client`
    to append_payroll_log — payroll_key if the config declares one,
    otherwise client_name — so rows key identically either way.
    ClientRegistry resolves payroll_key, client_name, canonical_name, and
    aliases through the same alias map, so one lookup covers all of them.
    """
    from parsers.base import _registry

    canonical = _registry.resolve(client_key)
    if not canonical:
        return None, None
    cfg = _registry.get_config(canonical)
    return cfg.get("payroll_key") or cfg.get("client_name"), cfg.get("client_name")


def main():
    args = sys.argv[1:]
    if len(args) != 3:
        print(__doc__)
        sys.exit(1)

    client_key_arg, check_date_raw, bank_credit_raw = args

    dispatch_key, client_name = _find_client(client_key_arg)
    if dispatch_key is None:
        print(f"ERROR: '{client_key_arg}' not found in any client config "
              "(checked payroll_key, client_name, canonical_name, aliases).")
        sys.exit(1)

    try:
        bank_credit = round(float(bank_credit_raw.replace(",", "").replace("$", "")), 2)
    except ValueError:
        print(f"ERROR: '{bank_credit_raw}' is not a valid dollar amount.")
        sys.exit(1)

    _assert_known_client(dispatch_key)
    check_date = _normalize_date(check_date_raw)
    ts = _now_pst().strftime("%Y-%m-%d %H:%M:%S")

    # 1. payroll_log.csv  (key: client, check_date — one row per division/date)
    payroll_entry = {
        "client":        dispatch_key,
        "client_name":   client_name,
        "check_date":    check_date,
        "bank_credit":   f"{bank_credit:.2f}",
        "balanced":      "N/A (marked done — not parsed from PDFs)",
        "run_timestamp": ts,
    }
    replaced = _upsert_csv(PAYROLL_LOG_PATH, PAYROLL_LOG_FIELDS,
                           ["client", "check_date"], payroll_entry)
    verb = "Updated" if replaced else "Logged"
    print(f"  📋 {verb} → payroll_log.csv  ({check_date}  ${bank_credit:,.2f})")

    # 2. reconciliation_log.csv (tracker reads this one; key: client, account_type)
    normalized_key = _normalize_client_key(dispatch_key)
    recon_entry = {
        "client":            normalized_key,
        "client_name":       client_name,
        "account_type":      "payroll",
        "account_ending":    "",
        "statement_date":    check_date,
        "beginning_balance": "",
        "ending_balance":    "",
        "total_payments":    "",
        "source":            "mark_payroll_done",
        "run_timestamp":     ts,
    }
    _upsert_csv(RECON_LOG_PATH, RECON_LOG_FIELDS,
               ["client", "account_type"], recon_entry)
    print(f"  📋 Logged → reconciliation_log.csv  (payroll  {check_date})")

    # 3. recon_log.json (digest "Reconciliation Runs" section)
    upsert_recon_log(
        client             = client_name,
        account_type       = "payroll",
        statement_end_date = check_date,
        ending_balance     = f"{bank_credit:.2f}",
        status             = "DONE",
    )
    print("  📝 Digest log → recon_log.json")

    _git_push_logs(f"{client_name} payroll {check_date} (mark_payroll_done)")

    print("\n✅ Done.")


if __name__ == "__main__":
    main()
