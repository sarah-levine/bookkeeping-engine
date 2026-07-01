"""
drive_archiver.py
-----------------
Archives reconciled statement PDFs to Google Drive, organized by
client / account type. Prevents duplicate uploads.

Folder structure:
  <ROOT_FOLDER>/
    <Client Name>/
      <Account Type>/
        statement_2026-02-06.pdf

Uses the same service account credentials as sheets_updater.py.
The service account must have Editor access on the target Drive folders.

Usage (standalone dry-run test):
    python3 drive_archiver.py --dry-run <pdf_path> <client_name> <account_type> [<date>]
"""

import json
import os
from pathlib import Path

from log_utils import load_private_json, get_clients_dir

SCOPES = ["https://www.googleapis.com/auth/drive"]

# Root folder IDs — configured in sheets_config.json
_CONFIG = load_private_json("sheets_config.json", default={})
STATEMENTS_ROOT = _CONFIG.get("drive_statements_folder", "")
FIXTURES_ROOT = _CONFIG.get("drive_fixtures_folder", "")


def _get_service():
    """Build the Drive API service. Credential resolution order:

    1. DRIVE_TOKEN_B64 env var — base64-encoded OAuth pickle, for sandboxed
       sessions without filesystem access. Generate with:
           python3 tools/export_drive_token.py
    2. drive_token.pickle file in BOOKKEEPING_CLIENTS_DIR — standard local
       OAuth token; auto-refreshed when expired and written back to disk.
    3. GOOGLE_SHEETS_CREDENTIALS env var or sheets_credentials.json — service
       account key. Works for uploads once the target Drive folder has Editor
       access granted to the service account email (one-time step in Drive UI).
    4. drive_credentials.json — interactive OAuth flow, first-run only.
    """
    import base64
    import pickle
    from googleapiclient.discovery import build

    clients_dir = Path(
        os.environ.get("BOOKKEEPING_CLIENTS_DIR")
        or str(Path.home() / ".bookkeeping" / "clients")
    )
    token_path = clients_dir / "drive_token.pickle"

    creds = None
    from_b64 = False

    # ── 1. Injected base64 token (sandboxed / CI sessions) ─────────────────
    token_b64 = os.environ.get("DRIVE_TOKEN_B64", "").strip()
    if token_b64:
        creds = pickle.loads(base64.b64decode(token_b64))
        from_b64 = True

    # ── 2. Token pickle file (standard local session) ───────────────────────
    if not creds and token_path.exists():
        with open(token_path, "rb") as f:
            creds = pickle.load(f)

    # Auto-refresh expired OAuth tokens (sources 1 and 2)
    if creds and not creds.valid and getattr(creds, "expired", False) and getattr(creds, "refresh_token", None):
        from google.auth.transport.requests import Request
        creds.refresh(Request())
        if not from_b64:
            with open(token_path, "wb") as f:
                pickle.dump(creds, f)

    # ── 3. Service account (no expiry; works if Drive folder shared with SA) ─
    if not creds or not creds.valid:
        creds_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS")
        if not creds_json:
            sa_path = clients_dir / "sheets_credentials.json"
            if sa_path.exists():
                creds_json = sa_path.read_text()
        if creds_json:
            from google.oauth2 import service_account
            info = json.loads(creds_json)
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=SCOPES
            )

    # ── 4. Interactive OAuth flow (first run) ───────────────────────────────
    if not creds or not creds.valid:
        oauth_creds_path = clients_dir / "drive_credentials.json"
        if oauth_creds_path.exists():
            from google_auth_oauthlib.flow import InstalledAppFlow
            flow = InstalledAppFlow.from_client_secrets_file(
                str(oauth_creds_path), SCOPES
            )
            creds = flow.run_local_server(port=0)
            with open(token_path, "wb") as f:
                pickle.dump(creds, f)
        else:
            raise EnvironmentError(
                "No Drive credentials found. Options:\n"
                "  • Set DRIVE_TOKEN_B64 (base64 OAuth token — run tools/export_drive_token.py)\n"
                "  • Set GOOGLE_SHEETS_CREDENTIALS (service account; share Drive folder with SA email)\n"
                "  • Place drive_credentials.json in BOOKKEEPING_CLIENTS_DIR for interactive auth"
            )

    return build("drive", "v3", credentials=creds)


def _find_or_create_folder(service, name: str, parent_id: str, dry_run: bool = False) -> str:
    """Find a subfolder by name under parent_id, or create it."""
    query = (
        f"'{parent_id}' in parents "
        f"and name = '{name}' "
        f"and mimeType = 'application/vnd.google-apps.folder' "
        f"and trashed = false"
    )
    results = service.files().list(
        q=query, fields="files(id, name)", pageSize=1
    ).execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]

    if dry_run:
        print(f"  [dry-run] Would create folder: {name} (under {parent_id})")
        return f"DRY_RUN_FOLDER_{name}"

    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]


def _list_files(service, folder_id: str) -> list[dict]:
    """List PDF files in a folder, sorted newest first."""
    query = (
        f"'{folder_id}' in parents "
        f"and mimeType = 'application/pdf' "
        f"and trashed = false"
    )
    results = service.files().list(
        q=query, fields="files(id, name, createdTime)",
        orderBy="createdTime desc", pageSize=100
    ).execute()
    return results.get("files", [])


def _file_exists(service, name: str, folder_id: str) -> bool:
    """Check if a file with the given name already exists in the folder."""
    query = (
        f"'{folder_id}' in parents "
        f"and name = '{name}' "
        f"and trashed = false"
    )
    results = service.files().list(
        q=query, fields="files(id)", pageSize=1
    ).execute()
    return len(results.get("files", [])) > 0


def _prune_old_statements(service, folder_id: str, keep: int = 2, dry_run: bool = False) -> None:
    """Keep only the most recent `keep` PDFs in a folder, delete the rest."""
    files = _list_files(service, folder_id)
    if len(files) <= keep:
        return
    for old in files[keep:]:
        if dry_run:
            print(f"  [dry-run] Would delete: {old['name']}")
        else:
            service.files().delete(fileId=old["id"]).execute()
            print(f"  🗑  Drive: deleted old statement — {old['name']}")


def _build_target_name(client_name: str, account_type: str, statement_date: str) -> str:
    """Build a clean filename for the archived PDF."""
    if statement_date:
        from log_utils import _normalize_date_iso
        date_clean = _normalize_date_iso(statement_date)
        return f"{client_name}_{account_type}_{date_clean}.pdf"
    return ""


def archive_statement(
    pdf_path: str,
    client_name: str,
    account_type: str,
    statement_date: str = "",
    dry_run: bool = False,
) -> str | None:
    """Upload a statement PDF to Drive under Client/AccountType/.

    Returns the Drive file ID, or None if skipped/failed.
    Skips if the file already exists in the target folder (dedup).
    Keeps only the 2 most recent statements per folder, deleting older ones.
    """
    root_id = STATEMENTS_ROOT
    if not root_id:
        print("  ⚠ Drive archive: no drive_statements_folder configured")
        return None

    pdf = Path(pdf_path)
    if not pdf.exists():
        print(f"  ⚠ Drive archive: file not found: {pdf_path}")
        return None

    target_name = _build_target_name(client_name, account_type, statement_date) or pdf.name

    # Sanitize folder names
    client_folder = client_name.strip().title()
    account_folder = account_type.strip().replace("_", " ").title()

    try:
        service = _get_service()

        # Navigate/create folder structure: Root / Client / Account Type
        client_id = _find_or_create_folder(service, client_folder, root_id, dry_run=dry_run)
        account_id = _find_or_create_folder(service, account_folder, client_id, dry_run=dry_run)

        # Dedup check (skip for dry-run folders that don't exist yet)
        if not account_id.startswith("DRY_RUN_") and _file_exists(service, target_name, account_id):
            print(f"  📁 Drive: already exists — {client_folder}/{account_folder}/{target_name}")
            return None

        if dry_run:
            print(f"  [dry-run] Would upload: {client_folder}/{account_folder}/{target_name}")
            _prune_old_statements(service, account_id, keep=2, dry_run=True)
            return "DRY_RUN"

        # Upload
        from googleapiclient.http import MediaFileUpload
        metadata = {"name": target_name, "parents": [account_id]}
        media = MediaFileUpload(str(pdf), mimetype="application/pdf", resumable=True)
        uploaded = service.files().create(
            body=metadata, media_body=media, fields="id"
        ).execute()
        file_id = uploaded["id"]
        print(f"  📁 Drive: archived → {client_folder}/{account_folder}/{target_name}")

        # Prune: keep only the 2 most recent statements
        _prune_old_statements(service, account_id, keep=2)

        return file_id

    except Exception as e:
        print(f"  ⚠ Drive archive failed: {e}")
        return None


def archive_fixture(
    pdf_path: str,
    client_name: str,
    account_type: str,
    dry_run: bool = False,
) -> str | None:
    """Upload a test fixture PDF to Drive under Client/AccountType/.

    Same structure as archive_statement but uses the fixtures root folder.
    No pruning — keeps all fixtures.
    """
    root_id = FIXTURES_ROOT
    if not root_id:
        return None

    pdf = Path(pdf_path)
    if not pdf.exists():
        return None

    target_name = pdf.name
    client_folder = client_name.strip().title()
    account_folder = account_type.strip().replace("_", " ").title()

    try:
        service = _get_service()
        client_id = _find_or_create_folder(service, client_folder, root_id, dry_run=dry_run)
        account_id = _find_or_create_folder(service, account_folder, client_id, dry_run=dry_run)

        if not account_id.startswith("DRY_RUN_") and _file_exists(service, target_name, account_id):
            print(f"  SKIP (already exists): {client_folder}/{account_folder}/{target_name}")
            return None

        if dry_run:
            print(f"  [dry-run] Would upload: {client_folder}/{account_folder}/{target_name}")
            return "DRY_RUN"

        from googleapiclient.http import MediaFileUpload
        metadata = {"name": target_name, "parents": [account_id]}
        media = MediaFileUpload(str(pdf), mimetype="application/pdf", resumable=True)
        uploaded = service.files().create(
            body=metadata, media_body=media, fields="id"
        ).execute()
        print(f"  UPLOADED: {client_folder}/{account_folder}/{target_name} -> {uploaded['id']}")
        return uploaded["id"]

    except Exception as e:
        print(f"  ⚠ Fixture upload failed: {e}")
        return None


if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    if len(args) < 3:
        print("Usage: python3 drive_archiver.py [--dry-run] <pdf_path> <client_name> <account_type> [<date>]")
        sys.exit(1)
    pdf, client, acct = args[0], args[1], args[2]
    date = args[3] if len(args) > 3 else ""
    archive_statement(pdf, client, acct, date, dry_run=dry)
