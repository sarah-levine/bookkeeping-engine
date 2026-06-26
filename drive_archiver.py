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
    """Build the Drive API service from service account credentials."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS")
    if not creds_json:
        clients_dir = os.environ.get("BOOKKEEPING_CLIENTS_DIR") or str(
            Path.home() / ".bookkeeping" / "clients"
        )
        clients_creds = Path(clients_dir) / "sheets_credentials.json"
        if clients_creds.exists():
            creds_json = clients_creds.read_text()
    if not creds_json:
        creds_file = Path(__file__).parent / "credentials.json"
        if not creds_file.exists():
            raise EnvironmentError(
                "No Google credentials found (GOOGLE_SHEETS_CREDENTIALS, "
                "sheets_credentials.json, or credentials.json)"
            )
        creds_json = creds_file.read_text()

    info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds)


def _find_or_create_folder(service, name: str, parent_id: str) -> str:
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

    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]


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


def _prune_old_statements(service, folder_id: str, keep: int = 2) -> None:
    """Keep only the most recent `keep` PDFs in a folder, delete the rest."""
    query = (
        f"'{folder_id}' in parents "
        f"and mimeType = 'application/pdf' "
        f"and trashed = false"
    )
    results = service.files().list(
        q=query, fields="files(id, name, createdTime)",
        orderBy="createdTime desc", pageSize=100
    ).execute()
    files = results.get("files", [])
    if len(files) <= keep:
        return
    for old in files[keep:]:
        service.files().delete(fileId=old["id"]).execute()
        print(f"  🗑  Drive: deleted old statement — {old['name']}")


def archive_statement(
    pdf_path: str,
    client_name: str,
    account_type: str,
    statement_date: str = "",
) -> str | None:
    """Upload a statement PDF to Drive under Client/AccountType/.

    Returns the Drive file ID, or None if skipped/failed.
    Skips if the file already exists in the target folder (dedup).
    Keeps only the 2 most recent statements per folder, deleting older ones.
    """
    root_id = STATEMENTS_ROOT
    if not root_id:
        return None

    pdf = Path(pdf_path)
    if not pdf.exists():
        print(f"  ⚠ Drive archive: file not found: {pdf_path}")
        return None

    # Build a clean filename: {client}_{account}_{date}.pdf
    if statement_date:
        from log_utils import _normalize_date_iso
        date_clean = _normalize_date_iso(statement_date)
        target_name = f"{client_name}_{account_type}_{date_clean}.pdf"
    else:
        target_name = pdf.name

    # Sanitize folder names
    client_folder = client_name.strip().title()
    account_folder = account_type.strip().replace("_", " ").title()

    try:
        service = _get_service()

        # Navigate/create folder structure: Root / Client / Account Type
        client_id = _find_or_create_folder(service, client_folder, root_id)
        account_id = _find_or_create_folder(service, account_folder, client_id)

        # Dedup check
        if _file_exists(service, target_name, account_id):
            print(f"  📁 Drive: already exists — {client_folder}/{account_folder}/{target_name}")
            return None

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
) -> str | None:
    """Upload a test fixture PDF to Drive under Client/AccountType/.

    Same structure as archive_statement but uses the fixtures root folder.
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
        client_id = _find_or_create_folder(service, client_folder, root_id)
        account_id = _find_or_create_folder(service, account_folder, client_id)

        if _file_exists(service, target_name, account_id):
            print(f"  SKIP (already exists): {client_folder}/{account_folder}/{target_name}")
            return None

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
