"""
sheets_updater.py
-----------------
Writes a reconciled statement date back to the Reconciliation Tracker
Google Sheet after each successful reconciliation run.

Requires:
  - GOOGLE_SHEETS_CREDENTIALS env var containing the service account JSON key
  - pip install google-auth google-auth-httplib2 google-api-python-client

Client-specific configuration (spreadsheet id, the fixed-cell map, and the
client/account label tables) lives in a private sheets_config.json kept out of
the public repo. See sheets_config.example.json for the structure. Resolution
order is handled by log_utils.load_private_json (private clients dir → repo →
committed example).
"""

import os
import json
from log_utils import load_private_json

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_CONFIG = load_private_json("sheets_config.json", default={})

SPREADSHEET_ID = _CONFIG.get("spreadsheet_id", "")
SHEET_NAME     = _CONFIG.get("sheet_name", "Sheet1")

# Map (client_key, account_type_key) → A1 cell reference. Stored in JSON with
# "CLIENT_KEY|account_type" string keys; split back into tuples here.
CELL_MAP = {
    tuple(k.split("|", 1)): v for k, v in _CONFIG.get("cell_map", {}).items()
}

# Normalize parser.client_name.upper().replace(' ', '_') → CELL_MAP canonical key.
# Catches long legal names and short aliases that miss the CELL_MAP lookup.
_CLIENT_KEY_MAP = _CONFIG.get("client_key_map", {})

# Normalize stmt_type (from reconcile_comprehensive.py) → CELL_MAP account key.
_ACCT_TYPE_MAP = _CONFIG.get("acct_type_map", {})


def _get_service():
    """Build the Sheets API service from env var or credentials.json fallback."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from pathlib import Path

    info = None
    creds_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS")
    if creds_json:
        try:
            info = json.loads(creds_json)
        except json.JSONDecodeError:
            info = None  # malformed env var — fall back to file-based credentials

    if info is None:
        # Auto-load from Bookkeeping-clients if available
        clients_dir = os.environ.get("BOOKKEEPING_CLIENTS_DIR") or str(Path.home() / ".bookkeeping" / "clients")
        clients_creds = Path(clients_dir) / "sheets_credentials.json"
        if clients_creds.exists():
            info = json.loads(clients_creds.read_text())

    if info is None:
        creds_file = Path(__file__).parent / "credentials.json"
        if not creds_file.exists():
            raise EnvironmentError(
                "GOOGLE_SHEETS_CREDENTIALS env var not set/invalid and credentials.json not found in repo root"
            )
        with open(creds_file) as f:
            info = json.load(f)

    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    import requests
    from google.auth.transport.requests import Request as _Req
    session = requests.Session()
    session.verify = False
    import urllib3; urllib3.disable_warnings()
    from google_auth_httplib2 import AuthorizedHttp
    import httplib2
    http = AuthorizedHttp(creds, http=httplib2.Http(disable_ssl_certificate_validation=True))
    return build("sheets", "v4", http=http, cache_discovery=False)


def update_sheet(client_key: str, account_type: str, date_str: str) -> bool:
    """
    Write date_str into the correct cell for client_key + account_type.
    Returns True on success, False if the cell mapping is unknown or write fails.
    date_str should be MM/DD/YY or MM/DD/YYYY.
    """
    norm_client  = _CLIENT_KEY_MAP.get(client_key, client_key)
    norm_account = _ACCT_TYPE_MAP.get(account_type, account_type)
    cell = CELL_MAP.get((norm_client, norm_account))
    if not cell:
        print(f"  ℹ️  No sheet cell mapped for ({norm_client}, {norm_account}) — skipping sheet update")
        return False
    try:
        svc    = _get_service()
        range_ = f"{SHEET_NAME}!{cell}"
        body   = {"values": [[date_str]]}
        svc.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=range_,
            valueInputOption="USER_ENTERED",
            body=body,
        ).execute()
        print(f"  📊 Sheet updated → {cell} = {date_str}")
        return True
    except Exception as e:
        print(f"  ⚠️  Sheet update failed for {cell}: {e}")
        return False

# Reverse map: cell → (client_key, account_type)
CELL_MAP_REVERSE = {v: k for k, v in CELL_MAP.items()}

# Human-readable client names for each client_key
CLIENT_NAMES = _CONFIG.get("client_names", {})


def sync_log_from_sheet() -> int:
    """
    Read every cell in CELL_MAP from the Google Sheet. For any cell that has
    a date but no matching entry in reconciliation_log.csv, write a row with
    source='gsheet' and blank balances.

    Returns the number of rows added.
    """
    import csv
    import re
    from datetime import datetime
    from pathlib import Path
    from log_utils import get_logs_dir

    # reconciliation_log.csv lives in the private logs dir, not the public repo.
    log_path = get_logs_dir() / "reconciliation_log.csv"

    # Load existing log entries into a set of (client_key, account_type, statement_date)
    existing = set()
    if log_path.exists():
        with open(log_path, newline='') as f:
            reader = csv.DictReader(f)
            for row in reader:
                existing.add((row['client'], row['account_type'], row['statement_date'].strip()))

    # Read all cells from the sheet in one batch request
    all_cells = list(CELL_MAP.values())
    ranges = [f"{SHEET_NAME}!{cell}" for cell in all_cells]

    try:
        svc = _get_service()
        result = svc.spreadsheets().values().batchGet(
            spreadsheetId=SPREADSHEET_ID,
            ranges=ranges,
        ).execute()
    except Exception as e:
        print(f"  ⚠️  Could not read sheet: {e}")
        return 0

    value_ranges = result.get('valueRanges', [])

    rows_added = 0
    new_rows = []

    for vr in value_ranges:
        range_str = vr.get('range', '')
        values = vr.get('values', [])
        if not values or not values[0]:
            continue

        date_str = values[0][0].strip()
        if not date_str:
            continue

        # Extract cell reference (e.g. "Sheet1!B4" → "B4")
        cell_ref = range_str.split('!')[-1].replace('$', '')
        # Normalize to just the cell (strip any range suffix)
        cell_ref = re.sub(r':.*', '', cell_ref)

        mapping = CELL_MAP_REVERSE.get(cell_ref)
        if not mapping:
            continue

        client_key, account_type = mapping
        client_name = CLIENT_NAMES.get(client_key, client_key)

        key = (client_key, account_type, date_str)
        if key in existing:
            continue

        new_rows.append({
            'client':            client_key,
            'client_name':       client_name,
            'account_type':      account_type,
            'account_ending':    '',
            'statement_date':    date_str,
            'beginning_balance': '',
            'ending_balance':    '',
            'total_payments':    '',
            'source':            'gsheet',
            'run_timestamp':     datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        })
        rows_added += 1

    if new_rows:
        file_exists = log_path.exists()
        with open(log_path, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=[
                'client','client_name','account_type','account_ending',
                'statement_date','beginning_balance','ending_balance',
                'total_payments','source','run_timestamp'
            ])
            if not file_exists:
                writer.writeheader()
            writer.writerows(new_rows)

        for row in new_rows:
            print(f"  📋 gsheet → log: {row['client_name']} | {row['account_type']} | {row['statement_date']}")

    return rows_added


# ═══════════════════════════════════════════════════════════════════════════
# Recon Log tab — append-only alternative to the fixed-cell grid
# ═══════════════════════════════════════════════════════════════════════════
# The fixed-cell CELL_MAP above is brittle: it requires the reconciliation run's
# (client_key, account_type) to exactly match a hardcoded cell, and silently
# skips the write on any mismatch. The "Recon Log" tab sidesteps that entirely —
# every reconciliation just appends a row, so a name/account variant shows up as
# a (possibly mislabeled) row instead of a silent no-op.

RECON_LOG_TAB    = "Recon Log"
RECON_LOG_HEADER = ["Client", "Account", "Statement Date", "Reconciled On"]

# Friendly, stable labels so variant client_name / stmt_type forms collapse to
# one consistent label. A miss here just means the raw value is logged — never a
# silent skip, which is the whole point.
ACCOUNT_LABELS = _CONFIG.get("account_labels", {})

# Normalize the many client_name forms (canonical, full legal, digest short)
# down to one display label. Keyed by client_name.upper().replace(' ', '_').
CLIENT_LABEL_ALIASES = _CONFIG.get("client_label_aliases", {})


def normalize_client_label(client_name: str) -> str:
    """Collapse a client_name (any variant) to a stable display label."""
    key = (client_name or "").strip().upper().replace(" ", "_")
    return CLIENT_LABEL_ALIASES.get(key, (client_name or "").strip())


def append_recon_row(client_name: str, account_type: str, statement_date: str) -> bool:
    """Append one row to the 'Recon Log' tab. No cell mapping, no silent skips."""
    from datetime import datetime
    client  = normalize_client_label(client_name)
    account = ACCOUNT_LABELS.get(account_type, account_type)
    reconciled_on = datetime.now().strftime("%Y-%m-%d")
    try:
        svc = _get_service()
        svc.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{RECON_LOG_TAB}!A:D",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [[client, account, statement_date, reconciled_on]]},
        ).execute()
        print(f"  📊 Recon Log → {client} | {account} | {statement_date}")
        return True
    except Exception as e:
        print(f"  ⚠️  Recon Log append failed: {e}")
        return False


def ensure_recon_log_tab() -> int:
    """Create the 'Recon Log' tab (with header row) if it doesn't exist.
    Returns the tab's gid. Run once before relying on append_recon_row()."""
    svc  = _get_service()
    meta = svc.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    for sh in meta.get("sheets", []):
        props = sh.get("properties", {})
        if props.get("title") == RECON_LOG_TAB:
            gid = props.get("sheetId")
            print(f"Tab '{RECON_LOG_TAB}' already exists (gid={gid}).")
            print(f"Link: https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit#gid={gid}")
            return gid

    resp = svc.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": [{"addSheet": {"properties": {"title": RECON_LOG_TAB}}}]},
    ).execute()
    gid = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
    svc.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{RECON_LOG_TAB}!A1:D1",
        valueInputOption="USER_ENTERED",
        body={"values": [RECON_LOG_HEADER]},
    ).execute()
    print(f"✅ Created tab '{RECON_LOG_TAB}' (gid={gid}).")
    print(f"Link: https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit#gid={gid}")
    return gid


def backfill_tracker():
    """Write tracker dates into the fixed-cell grid using the same logic as the email digest."""
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.dirname(__file__))
    from send_morning_digest import TRACKER, get_tracker_date, load_reconciliation_log

    recon_dates = load_reconciliation_log()
    updates = []
    for client in TRACKER:
        name = client["client"]
        for acct in client["accounts"]:
            date_str = get_tracker_date(recon_dates, name, client["client_keys"], acct)
            if not date_str or date_str == "—":
                continue
            cell = None
            for ck in client["client_keys"]:
                norm_ck = _CLIENT_KEY_MAP.get(ck, ck)
                norm_at = _ACCT_TYPE_MAP.get(acct["key"], acct["key"])
                cell = CELL_MAP.get((norm_ck, norm_at))
                if cell:
                    break
            if not cell:
                print(f"  ⚠️  No cell mapped for {name} / {acct['label']} ({acct['key']}) — skipping")
                continue
            updates.append({"range": f"{SHEET_NAME}!{cell}", "values": [[date_str]]})

    if not updates:
        print("Nothing to update.")
        return

    svc = _get_service()
    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"valueInputOption": "USER_ENTERED", "data": updates},
    ).execute()
    print(f"✅ Backfilled {len(updates)} cells into the tracker grid.")
    for u in updates:
        print(f"   {u['range']:25s} → {u['values'][0][0]}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--create-tab":
        ensure_recon_log_tab()
    elif len(sys.argv) > 1 and sys.argv[1] == "--backfill":
        backfill_tracker()
    else:
        print("Usage:")
        print("  python3 sheets_updater.py --create-tab   # create Recon Log tab")
        print("  python3 sheets_updater.py --backfill     # populate tracker grid from known dates")
