"""
drive_fixtures.py
-----------------
Download test-fixture PDFs from Google Drive at runtime so the repo never has
to store real client statements. Real PDFs live in a private Drive folder
shared with the service account; this module pulls them into a local cache
when tests run, and the tests skip gracefully when Drive isn't reachable
(e.g. on a public CI runner with no credentials).

Auth: reuses the same service-account JSON as sheets_updater.py.
  - GOOGLE_SHEETS_CREDENTIALS env var (service-account JSON), or
  - credentials.json in the repo root
The service account needs read access to the fixture files — share the Drive
fixtures folder with the service account's client_email.

Requires the Drive API enabled in the GCP project and:
  pip install google-auth google-api-python-client
"""

import os
import json
from pathlib import Path

SCOPES     = ["https://www.googleapis.com/auth/drive.readonly"]
CACHE_DIR  = Path(__file__).parent / ".fixture_cache"


class DriveUnavailable(Exception):
    """Raised when Drive credentials or connectivity are missing."""


def _load_credentials_info():
    creds_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS")
    if creds_json:
        return json.loads(creds_json)
    # Check GOOGLE_SERVICE_ACCOUNT_FILE (file path) — same env var used by
    # reconcile_comprehensive.py --from-drive
    sa_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "")
    if sa_file and Path(sa_file).exists():
        with open(sa_file) as f:
            return json.load(f)
    creds_file = Path(__file__).parent.parent / "credentials.json"
    if creds_file.exists():
        with open(creds_file) as f:
            return json.load(f)
    raise DriveUnavailable(
        "No Drive credentials: set GOOGLE_SHEETS_CREDENTIALS or GOOGLE_SERVICE_ACCOUNT_FILE or add credentials.json"
    )


def _get_service():
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as e:
        raise DriveUnavailable(f"Google client libraries not installed: {e}")

    info  = _load_credentials_info()
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

    # Mirror sheets_updater.py: tolerate environments with SSL interception.
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    import urllib3
    urllib3.disable_warnings()
    http = AuthorizedHttp(creds, http=httplib2.Http(disable_ssl_certificate_validation=True))
    return build("drive", "v3", http=http, cache_discovery=False)


def fetch_pdf(file_id: str, cache_name: str | None = None) -> Path:
    """Download a Drive file by ID into the local cache and return its path.

    Cached downloads are reused. Raises DriveUnavailable if Drive can't be
    reached so callers (tests) can skip instead of failing hard.
    """
    CACHE_DIR.mkdir(exist_ok=True)
    dest = CACHE_DIR / (cache_name or f"{file_id}.pdf")
    if dest.exists() and dest.stat().st_size > 0:
        return dest

    try:
        from googleapiclient.http import MediaIoBaseDownload
    except ImportError as e:
        raise DriveUnavailable(f"Google client libraries not installed: {e}")

    import io
    svc = _get_service()
    request = svc.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
    dest.write_bytes(buf.getvalue())
    return dest


def drive_available() -> bool:
    """Cheap check used by tests to decide whether to run or skip."""
    try:
        _load_credentials_info()
        return True
    except DriveUnavailable:
        return False


def fetch_pdf_entry(entry: dict, cache_name: str | None = None) -> Path:
    """Fetch a fixture from Drive or a local clients-dir path.

    Manifest entries may declare:
      source: "drive"  (default) — download by file_id from Google Drive
      source: "repo"             — read directly from BOOKKEEPING_CLIENTS_DIR

    For source='repo', `path` is relative to the clients dir and defaults to
    fixtures/fixture_<name>.pdf.  No Drive credentials required.
    """
    if entry.get("source") == "repo":
        try:
            from log_utils import get_clients_dir
            clients_dir = get_clients_dir()
        except ImportError:
            import os as _os
            p = _os.environ.get("BOOKKEEPING_CLIENTS_DIR", "")
            if not p:
                raise DriveUnavailable(
                    "source='repo' but log_utils unavailable and BOOKKEEPING_CLIENTS_DIR not set"
                )
            clients_dir = Path(p)
        rel   = entry.get("path", f"fixtures/fixture_{entry['name']}.pdf")
        local = clients_dir / rel
        if not local.exists():
            raise DriveUnavailable(f"Local fixture not found: {local}")
        return local
    return fetch_pdf(entry["file_id"], cache_name)
