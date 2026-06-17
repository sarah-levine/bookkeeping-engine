import sys
import re
import csv
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime

def _now_pst():
    """Return current datetime in US/Pacific (PST/PDT)."""
    return datetime.now(ZoneInfo('America/Los_Angeles'))

import pdfplumber

def extract_text(pdf_path: str) -> str:
    """Extract all text from a PDF file."""
    with pdfplumber.open(pdf_path) as pdf:
        return "\n".join(
            page.extract_text() for page in pdf.pages if page.extract_text()
        )


def amt(s) -> float:
    """Convert '$1,234.56' or '1,234.56' to float."""
    try:
        return round(float(re.sub(r'[$,]', '', str(s).strip())), 2)
    except (ValueError, AttributeError):
        return 0.0


def parse_header(text: str) -> dict:
    """Parse check date / pay period / company from ADP Payroll Details PDFs.
    Works for the whitespace-stripped style used by ADP Payroll Details reports. Also
    handles the 'Check dates from: X to: Y' / 'Pay Period from: X to: Y'
    header format used on multi-payroll combined reports."""
    header = {}
    # Single-payroll header: "Check date: 4/10/2026"
    m = re.search(r'Checkdate:(\d+/\d+/\d+)', text)
    if m:
        header["check_date"] = m.group(1)
    else:
        # Combined / multi-payroll header:
        #   "Check dates from: 5/8/2026 - Payroll 1 to: 5/14/2026 - Payroll 2"
        # Capture BOTH check dates — a combined report can span two pay dates,
        # and dropping the second silently mislabels half the journal.
        m = re.search(r'Checkdatesfrom:(\d+/\d+/\d+).*?to:(\d+/\d+/\d+)', text)
        if m:
            header["check_date"]     = m.group(1)
            header["check_date_end"] = m.group(2)
        else:
            m = re.search(r'Checkdatesfrom:(\d+/\d+/\d+)', text)
            if m: header["check_date"] = m.group(1)
    # Single-payroll: "Pay Period: 03/22/2026 to: 04/04/2026"
    m = re.search(r'PayPeriod:(\S+)to:(\S+)', text)
    if m:
        header["pay_period_start"] = m.group(1)
        header["pay_period_end"]   = m.group(2)
    else:
        # Combined header variant: "Pay Period from: 04/19/2026 to: 05/15/2026"
        m = re.search(r'PayPeriodfrom:(\d+/\d+/\d+)to:(\d+/\d+/\d+)', text)
        if m:
            header["pay_period_start"] = m.group(1)
            header["pay_period_end"]   = m.group(2)
    m = re.search(r'Company:(.+?)(?:Date Printed|\d+ of \d+)', text)
    if m: header["company"] = m.group(1).strip()
    return header


def verify_same_check_date(pdfs: dict) -> str:
    """Verify all provided ADP PDFs are from the same payroll period.

    pdfs: dict mapping a human label (e.g. 'Payroll Details', 'Payroll Liability')
          to a file path. Each PDF's check date is parsed from its footer.

    Returns the shared check date string on success.
    Exits with code 1 and a clear message if dates don't match or can't be parsed.
    """
    found = {}
    for label, path in pdfs.items():
        if not path:
            continue
        header = parse_header(extract_text(path))
        date   = header.get("check_date", "")
        if not date:
            print(f"⚠️  Could not parse check date from {label} PDF — skipping date verification")
            return ""
        found[label] = date
        # A combined report may itself span two check dates — treat that
        # internal spread as a mismatch so we never build a single-date journal
        # from a two-period PDF.
        end = header.get("check_date_end", "")
        if end and end != date:
            found[f"{label} (end)"] = end
    if not found:
        return ""
    unique = set(found.values())
    if len(unique) > 1:
        print("❌  CHECK DATE MISMATCH")
        for label, date in found.items():
            print(f"    {label} PDF: {date}")
        print("    These PDFs are from different payroll periods. Aborting.")
        sys.exit(1)
    return next(iter(unique))


def make_row(date, account, debit=None, credit=None, memo="", name="") -> dict:
    """Build a journal entry row dict with a Date column."""
    return {
        "Date":    date,
        "Account": account,
        "Debit":   f"{debit:.2f}"  if debit  is not None and debit  != "" else "",
        "Credit":  f"{credit:.2f}" if credit is not None and credit != "" else "",
        "Memo":    memo,
        "Name":    name,
    }


def check_balance(rows: list) -> tuple:
    total_d = round(sum(float(r["Debit"])  for r in rows if r["Debit"]),  2)
    total_c = round(sum(float(r["Credit"]) for r in rows if r["Credit"]), 2)
    return total_d, total_c


def print_journal_table(rows: list, client_name: str, check_date: str):
    """Print a formatted journal entry table."""
    # Shorten long account names for display
    def shorten(acct, width=40):
        return acct[:width-1] + "+" if len(acct) > width else acct
    print(f"\n{'=' * 72}")
    print(f"  {client_name}")
    print(f"  Payroll Journal Entry — {check_date}")
    print(f"{'=' * 72}")
    print(f"  {'Account':<40} {'Debit':>11} {'Credit':>11}  Memo")
    print(f"  {'-' * 68}")
    for r in rows:
        d = f"{float(r['Debit']):>11,.2f}"  if r["Debit"]  else f"{'':>11}"
        c = f"{float(r['Credit']):>11,.2f}" if r["Credit"] else f"{'':>11}"
        label = r["Memo"] or r.get("Name", "")
        print(f"  {shorten(r['Account']):<40} {d} {c}  {label}")
    total_d, total_c = check_balance(rows)
    print(f"  {'-' * 68}")
    print(f"  {'TOTALS':<40} {total_d:>11,.2f} {total_c:>11,.2f}")
    diff = round(total_d - total_c, 2)
    if abs(diff) < 0.02:
        print(f"\n  BALANCED  (${total_d:,.2f})")
    else:
        print(f"\n  OUT OF BALANCE by ${diff:,.2f}")
    print()


def write_csv(rows: list, output_path: str):
    """Write rows + totals to CSV."""
    fieldnames = ["Date", "Account", "Debit", "Credit", "Memo", "Name"]
    total_d, total_c = check_balance(rows)
    with open(output_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
        w.writerow({"Date": "", "Account": "TOTALS",
                    "Debit": f"{total_d:.2f}", "Credit": f"{total_c:.2f}",
                    "Memo": "", "Name": ""})


def write_iif(rows: list, client_name: str, check_date: str) -> str:
    """Write journal rows to a QuickBooks Desktop IIF file.

    Positive AMOUNT = debit, negative AMOUNT = credit (QB convention).
    Returns the filename written.
    """
    import re
    safe_client = re.sub(r"[^\w]", "_", client_name).strip("_")
    safe_date   = check_date.replace("/", "-")
    filename    = f"{safe_client}_{safe_date}.iif"

    lines = [
        "!TRNS\tTRNSTYPE\tDATE\tACCNT\tNAME\tAMOUNT\tMEMO",
        "!SPL\tTRNSTYPE\tDATE\tACCNT\tNAME\tAMOUNT\tMEMO",
        "!ENDTRNS",
    ]
    for i, row in enumerate(rows):
        tag    = "TRNS" if i == 0 else "SPL"
        amount = float(row["Debit"]) if row["Debit"] else (
                -float(row["Credit"]) if row["Credit"] else 0.0)
        name   = (row.get("Name") or "").strip()
        memo   = (row.get("Memo") or "").strip()
        lines.append(
            f"{tag}\tGENERAL JOURNAL\t{row['Date']}\t{row['Account']}"
            f"\t{name}\t{amount:.2f}\t{memo}"
        )
    lines.append("ENDTRNS")

    with open(filename, "w", newline="\r\n", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return filename


# ─────────────────────────────────────────────────────────────────────────────
# SHARED LOG UTILITIES  (payroll_log.csv  +  reconciliation_log.csv)
# ─────────────────────────────────────────────────────────────────────────────

# base.py lives in payroll_clients/, so the repo root is one level up.
REPO_DIR   = Path(__file__).parent.parent
# Operational logs hold real client data and live in the private logs dir
# (see log_utils.get_logs_dir), not the public repo.
import sys as _sys
_sys.path.insert(0, str(REPO_DIR))
from log_utils import get_logs_dir as _get_logs_dir
LOGS_DIR   = _get_logs_dir()
PAYROLL_LOG_PATH = LOGS_DIR / "payroll_log.csv"
RECON_LOG_PATH   = LOGS_DIR / "reconciliation_log.csv"

PAYROLL_LOG_FIELDS = ["client", "client_name", "check_date", "bank_credit",
                      "balanced", "run_timestamp"]
RECON_LOG_FIELDS   = ["client", "client_name", "account_type", "account_ending",
                      "statement_date", "beginning_balance", "ending_balance",
                      "total_payments", "source", "run_timestamp"]


def _upsert_csv(log_path: Path, fields: list, key_fields: list, entry: dict):
    """Write entry to a CSV log, replacing any existing row with the same key.

    key_fields  — column names that together identify a unique record
                  (e.g. ["client", "check_date"] for payroll,
                        ["client", "statement_date"] for reconciliation)
    """
    rows = []
    if log_path.exists():
        with open(log_path, newline="") as f:
            rows = list(csv.DictReader(f))

    key = {k: entry[k] for k in key_fields}
    replaced = False
    for i, row in enumerate(rows):
        if all(row.get(k) == key[k] for k in key_fields):
            rows[i] = entry
            replaced = True
            break
    if not replaced:
        rows.append(entry)

    with open(log_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)

    return replaced


def append_payroll_log(client: str, client_name: str, check_date: str, rows: list):
    """Upsert one row into payroll_log.csv and reconciliation_log.csv (last run wins for same client+date)."""
    from log_utils import _assert_known_client
    _assert_known_client(client)
    from datetime import datetime
    total_d, total_c = check_balance(rows)
    balanced = abs(round(total_d - total_c, 2)) < 0.02
    entry = {
        "client":        client,
        "client_name":   client_name,
        "check_date":    check_date,
        "bank_credit":   f"{total_c:.2f}",
        "balanced":      "TRUE" if balanced else "FALSE",
        "run_timestamp": _now_pst().strftime("%Y-%m-%d %H:%M:%S"),
    }
    replaced = _upsert_csv(PAYROLL_LOG_PATH, PAYROLL_LOG_FIELDS,
                           ["client", "check_date"], entry)
    verb = "Updated" if replaced else "Logged"
    print(f"  📋 {verb} → payroll_log.csv  ({check_date}  ${float(entry['bank_credit']):,.2f})")

    # Always write to reconciliation_log.csv — regardless of QB confirmation status.
    # The tracker reads from reconciliation_log only, so payroll dates must always land here.
    # Use _normalize_client_key so the key matches the cell_map regardless of config naming.
    from log_utils import _normalize_client_key
    normalized_key = _normalize_client_key(client)
    recon_entry = {
        "client":             normalized_key,
        "client_name":        client_name,
        "account_type":       "payroll",
        "account_ending":     "",
        "statement_date":     check_date,
        "beginning_balance":  "",
        "ending_balance":     "",
        "total_payments":     "",
        "run_timestamp":      _now_pst().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _upsert_csv(RECON_LOG_PATH, RECON_LOG_FIELDS,
                ["client", "account_type", "statement_date"], recon_entry)
    print(f"  📋 {verb} → reconciliation_log.csv  (payroll  {check_date})")


def _git_push_logs(label: str):
    """Best-effort: commit the log files and push so the cloud morning digest
    sees this run. Reconciliation already does this; payroll did not, which is
    why payroll runs kept missing the digest. Never raises."""
    import subprocess
    logs_dir = LOGS_DIR
    log_files = ["recon_log.json", "payroll_log.csv", "reconciliation_log.csv"]
    existing  = [f for f in log_files if (logs_dir / f).exists()]
    if not existing:
        return

    def _git(*args):
        return subprocess.run(["git", "-C", str(logs_dir), *args],
                              capture_output=True, text=True)

    try:
        # Commit only the log files (pathspec) — never sweep up other staged work.
        commit = _git("commit", "-m", f"digest: {label}", "--", *existing)
        if commit.returncode != 0:
            return  # nothing changed in the log files, or commit unavailable
        push = _git("push")
        if push.returncode == 0:
            print(f"  ☁️  Pushed logs to GitHub — morning digest will include this run")
        else:
            print(f"  ⚠ Git push failed: {push.stderr.strip()}")
            print(f"     Run: git pull --rebase && git push")
    except Exception as _e:
        print(f"  ⚠ Could not auto-push logs ({_e}). Commit/push manually.")


def append_digest_log(client_name: str, check_date: str):
    """Upsert a payroll run entry into recon_log.json
    so the morning digest picks it up under 'Reconciliation Runs'."""
    try:
        from log_utils import upsert_recon_log
        upsert_recon_log(
            client             = client_name,
            account_type       = "payroll",
            statement_end_date = check_date,
            status             = "CLEAN",
        )
        print(f"  📝 Digest log → recon_log.json")
        _git_push_logs(f"{client_name} payroll {check_date}")
    except Exception as _e:
        bar = "  " + "!" * 64
        print(bar)
        print(f"  ⚠️  WARNING: payroll for {client_name} ({check_date}) was NOT written")
        print(f"     to recon_log.json — it will be MISSING from the morning digest.")
        print(f"     Reason: {_e}")
        print(f"     (payroll_log.csv was still updated; only the digest log failed)")
        print(bar)


def append_recon_log(client: str, client_name: str, account_type: str,
                     account_ending: str, statement_date: str,
                     beginning_balance: str, ending_balance: str,
                     total_payments: str):
    """Upsert one row into reconciliation_log.csv (last run wins for same client+date)."""
    entry = {
        "client":             client,
        "client_name":        client_name,
        "account_type":       account_type,   # e.g. "amex_cc", "amex_checking", "chase_ink"
        "account_ending":     account_ending,
        "statement_date":     statement_date,
        "beginning_balance":  beginning_balance,
        "ending_balance":     ending_balance,
        "total_payments":     total_payments,
        "run_timestamp":      _now_pst().strftime("%Y-%m-%d %H:%M:%S"),
    }
    replaced = _upsert_csv(RECON_LOG_PATH, RECON_LOG_FIELDS,
                           ["client", "account_ending", "statement_date"], entry)
    verb = "Updated" if replaced else "Logged"
    print(f"  📋 {verb} → reconciliation_log.csv  ({statement_date}  ending ${ending_balance})")


def load_config(filename: str) -> dict:
    """Load a client JSON config from the clients directory.

    Respects BOOKKEEPING_CLIENTS_DIR env var → ~/.bookkeeping/clients/ → ./clients/ fallback.
    """
    from log_utils import get_clients_dir
    path = get_clients_dir() / filename
    with open(path) as f:
        return json.load(f)


# ═══════════════════════════════════════════════════════════════════════════
# QUICKBOOKS CONFIRMATION HELPERS
# ═══════════════════════════════════════════════════════════════════════════



def _qb_confirm(label: str) -> bool:
    """Prompt user to confirm QB entry. Returns True if done, False if later.
    
    If BOOKKEEPING_NO_PROMPT env var is set (via --no-prompt flag), auto-answers
    'later' so the script can run non-interactively from Claude's environment.
    """
    import os
    print()
    print('─' * 80)
    if os.environ.get('BOOKKEEPING_NO_PROMPT'):
        print(f'  [--no-prompt] Auto-answered: later — log written, sheet update deferred.')
        print('─' * 80)
        print()
        return False
    while True:
        answer = input(f'  Have you entered {label} into QuickBooks? (done / later): ').strip().lower()
        if answer in ('done', 'later'):
            break
        print('  Please type "done" when finished, or "later" to log now and update the sheet when done.')
    if answer == 'later':
        print('  📋 Logging now — sheet will update next time you run with "done".')
    print('─' * 80)
    print()
    return answer == 'done'

