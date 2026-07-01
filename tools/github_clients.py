"""
github_clients.py
-----------------
Read/write files in the private Bookkeeping-clients GitHub repo via the
REST API, using GITHUB_PAT_BOOKKEEPING.  Lets the remote Claude Code session
make log entries and push them back without needing a local clone.

Usage (programmatic):
    from tools.github_clients import pull_files, push_files, trigger_dispatch

    pull_files(['reconciliation_log.csv', 'recon_log.json', 'payroll_log.csv', 'acme_inc.json'])
    # ... make changes locally in ~/.bookkeeping/clients/ ...
    push_files(['reconciliation_log.csv', 'recon_log.json', 'payroll_log.csv'], "Log Acme Inc May 2026")
    trigger_dispatch()
"""

import os
import json
import base64
import urllib.request
import urllib.error
from pathlib import Path

REPO   = "sarah-levine/Bookkeeping-clients"
BRANCH = "main"
LOCAL_DIR = Path(os.environ.get("BOOKKEEPING_CLIENTS_DIR",
                                Path.home() / ".bookkeeping" / "clients"))

# ── internal helpers ────────────────────────────────────────────────────────

def _pat() -> str:
    pat = os.environ.get("GITHUB_PAT_BOOKKEEPING", "").strip()
    if not pat:
        raise EnvironmentError("GITHUB_PAT_BOOKKEEPING not set")
    return pat

def _api(path: str, method: str = "GET", body: dict = None):
    url = f"https://api.github.com/repos/{REPO}/contents/{path}"
    headers = {
        "Authorization": f"token {_pat()}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

# ── public API ───────────────────────────────────────────────────────────────

def pull_file(remote_path: str, local_name: str = None) -> Path:
    """Download one file from the private repo into LOCAL_DIR."""
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    data     = _api(remote_path)
    content  = base64.b64decode(data["content"]).decode("utf-8")
    local_path = LOCAL_DIR / (local_name or Path(remote_path).name)
    local_path.write_text(content, encoding="utf-8")
    return local_path


def pull_files(remote_paths: list[str]) -> dict[str, Path]:
    """Download multiple files. Returns {remote_path: local_path}."""
    results = {}
    for p in remote_paths:
        results[p] = pull_file(p)
        print(f"  ⬇  pulled {p}")
    return results


def push_file(remote_path: str, local_name: str = None, message: str = None) -> bool:
    """Upload a local file back to the private repo (creates or updates)."""
    local_path = LOCAL_DIR / (local_name or Path(remote_path).name)
    if not local_path.exists():
        raise FileNotFoundError(f"Local file not found: {local_path}")

    content_b64 = base64.b64encode(local_path.read_bytes()).decode()

    # Get current SHA (needed for updates)
    try:
        existing = _api(remote_path)
        sha = existing["sha"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            sha = None
        else:
            raise

    body = {
        "message": message or f"Update {remote_path}",
        "content": content_b64,
        "branch":  BRANCH,
    }
    if sha:
        body["sha"] = sha

    _api(remote_path, method="PUT", body=body)
    print(f"  ⬆  pushed {remote_path}")
    return True


def push_files(remote_paths: list[str], message: str) -> None:
    """Upload multiple files with the same commit message."""
    for p in remote_paths:
        push_file(p, message=message)


def trigger_dispatch() -> None:
    """Fire the logs-updated repository_dispatch to sync Google Sheets."""
    url = f"https://api.github.com/repos/{REPO}/dispatches"
    headers = {
        "Authorization": f"token {_pat()}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
    }
    body = json.dumps({"event_type": "logs-updated"}).encode()
    req  = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10):
        pass
    print("  📊 Sheet update triggered")


def pull_client_config(filename: str) -> dict:
    """Pull a client JSON config and return the parsed dict."""
    pull_file(filename)
    return json.loads((LOCAL_DIR / filename).read_text())


def list_files(subdir: str = "") -> list[str]:
    """List files in the repo (or a subdirectory)."""
    data = _api(subdir or "")
    if isinstance(data, list):
        return [f["name"] for f in data if f["type"] == "file"]
    return []


# ── high-level helpers ──────────────────────────────────────────────────────

_LOG_FILES = ["reconciliation_log.csv", "recon_log.json", "payroll_log.csv"]
_SHARED_CONFIGS = ["sheets_config.json", "digest_config.json",
                   "vendor_rules_global.json", "manual_statements.json"]


def _discover_client_configs() -> list[str]:
    """List client config filenames from the repo via the REST API."""
    try:
        all_files = list_files()
        return [f for f in all_files
                if f.endswith(".json")
                and f not in _LOG_FILES
                and f not in _SHARED_CONFIGS
                and not f.startswith("_")
                and f not in ("fixtures_manifest.json", "sheets_credentials.json",
                              "drive_credentials.json")]
    except Exception:
        return []


def sync_down(include_configs: bool = True) -> None:
    """Pull log files (and optionally client configs) from Bookkeeping-clients."""
    targets = list(_LOG_FILES)
    if include_configs:
        targets += _SHARED_CONFIGS + _discover_client_configs()
    for name in targets:
        try:
            pull_file(name)
            print(f"  ⬇  pulled {name}")
        except Exception as e:
            print(f"  ⚠  skipped {name}: {e}")


def sync_up(message: str, dispatch: bool = True) -> None:
    """Push log files back to Bookkeeping-clients and optionally trigger Sheet sync."""
    push_files(_LOG_FILES, message=message)
    if dispatch:
        trigger_dispatch()


def log_recon(
    *,
    client: str,
    client_name: str,
    account_type: str,
    statement_end_date: str,
    statement: str = "",
    beginning_balance: str,
    ending_balance: str,
    total_payments: str = "0.00",
    status: str = "DONE",
    commit_message: str = None,
    dispatch: bool = True,
) -> None:
    """Full cycle: pull logs → write_both_logs → push → dispatch.

    Pulls fresh copies of the log files and client configs first so the
    local state is always current before writing.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    os.environ.setdefault("BOOKKEEPING_CLIENTS_DIR", str(LOCAL_DIR))
    os.environ.setdefault("BOOKKEEPING_LOGS_DIR", str(LOCAL_DIR))
    os.environ["BOOKKEEPING_NO_PROMPT"] = "1"

    sync_down(include_configs=True)

    from log_utils import write_both_logs
    write_both_logs(
        client=client,
        client_name=client_name,
        account_type=account_type,
        statement_end_date=statement_end_date,
        statement=statement,
        beginning_balance=beginning_balance,
        ending_balance=ending_balance,
        total_payments=total_payments,
        status=status,
    )

    msg = commit_message or f"Log {client_name} {account_type} {statement_end_date} ({status})"
    sync_up(msg, dispatch=dispatch)
