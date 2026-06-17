#!/usr/bin/env python3
"""
upload_fixtures_to_drive.py
---------------------------
Upload local fixture PDFs to the Bookkeeping Test Fixtures Drive folder and
update tests/fixtures_manifest.json (and Bookkeeping-clients/fixtures_manifest.json
if BOOKKEEPING_CLIENTS_DIR is set) with the resulting file IDs.

Usage:
    # Migrate ALL locally-stored (source='repo') fixtures to Drive in one shot:
    python3 upload_fixtures_to_drive.py --migrate-repo

    # Or upload specific PDFs by hand:
    python3 upload_fixtures_to_drive.py <entry_name> <format> <pdf_path> [...]

    <entry_name>  unique name for this fixture, e.g. acme_bofa_credit
    <format>      parser format key, e.g. bofa_credit (see VALID_FORMATS below)
    <pdf_path>    path to the PDF on disk

    Repeat the triple for multiple uploads in one run.

Examples:
    python3 upload_fixtures_to_drive.py \\
        acme_bofa_credit   bofa_credit  ~/Downloads/acme_bofa_credit_jun26.pdf \\
        acme_bofa_savings  bofa_savings ~/Downloads/acme_bofa_savings_may26.pdf \\
        acme_bofa_checking bofa_checking ~/Downloads/acme_bofa_checking_may26.pdf

Auth: OAuth as your own Google account (service accounts have no Drive storage
  quota and can't create files in a personal My Drive folder — they can only
  read shared ones, which is why sheets_updater's service account isn't used for
  uploads here).
  - One-time: create a Desktop OAuth client in Google Cloud Console and save the
    JSON to ~/.bookkeeping/oauth_client.json (or set GOOGLE_OAUTH_CLIENT).
  - First run opens a browser for consent; the token is cached at
    ~/.bookkeeping/drive_token.json so later runs are non-interactive.

Drive folder: Bookkeeping/Bookkeeping Test Fixtures/
Folder ID: 17O16oCwRI7u0bSDD7kAb9s2H_JfVDjju
"""

import json
import os
import sys
from pathlib import Path

FIXTURES_FOLDER_ID = "17O16oCwRI7u0bSDD7kAb9s2H_JfVDjju"

REPO_DIR      = Path(__file__).parent
MANIFEST_PATH = REPO_DIR / "tests" / "fixtures_manifest.json"


def _clients_dir():
    """Resolve the private clients dir the same way the rest of the system does
    (BOOKKEEPING_CLIENTS_DIR → ~/.bookkeeping/clients → repo ./clients), so this
    script works without the env var set explicitly."""
    try:
        import sys as _sys
        _sys.path.insert(0, str(REPO_DIR))
        from log_utils import get_clients_dir
        return get_clients_dir()
    except Exception:
        env = os.environ.get("BOOKKEEPING_CLIENTS_DIR", "").strip()
        if env:
            return Path(env)
        home = Path.home() / ".bookkeeping" / "clients"
        return home if home.exists() else REPO_DIR / "clients"

VALID_FORMATS = {
    "bofa_checking", "bofa_credit", "bofa_savings",
    "citi_checking", "citi_savings", "citi_visa_costco",
    "chase_ink", "chase_united", "chase_sapphire",
    "amex", "amex_checking",
    "bmo_checking",
    "usbank_checking",
    "wells_fargo_credit", "wells_fargo_checking",
    "northern_trust_checking",
    "adp_payroll_detail", "adp_payroll_liability",
}


DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]


def _get_service():
    """Authenticate as the user via OAuth so uploaded files are owned by a real
    account with storage quota. (Service accounts have no My Drive quota and
    cannot create files in a personal Drive folder — only read shared ones.)

    Looks for an OAuth client secret (Desktop app) and caches the resulting
    token so the browser consent only happens once:
      client secret: GOOGLE_OAUTH_CLIENT  or  ~/.bookkeeping/oauth_client.json
      token cache:    GOOGLE_OAUTH_TOKEN   or  ~/.bookkeeping/drive_token.json
    """
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as e:
        print(f"ERROR: OAuth libraries not installed: {e}")
        print("  pip install google-auth-oauthlib google-api-python-client")
        sys.exit(1)

    cfg_dir     = Path.home() / ".bookkeeping"
    token_path  = Path(os.environ.get("GOOGLE_OAUTH_TOKEN",  str(cfg_dir / "drive_token.json")))
    client_path = Path(os.environ.get("GOOGLE_OAUTH_CLIENT", str(cfg_dir / "oauth_client.json")))

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), DRIVE_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not client_path.exists():
                print(f"ERROR: OAuth client secret not found at {client_path}")
                print("  One-time setup:")
                print("   1. Google Cloud Console → APIs & Services → Credentials")
                print("   2. Create Credentials → OAuth client ID → Application type: Desktop app")
                print("   3. Download the JSON and save it to that path")
                print("      (or set GOOGLE_OAUTH_CLIENT to wherever you put it).")
                print("   Make sure the Drive API is enabled and your email is a test user")
                print("   on the OAuth consent screen.")
                sys.exit(1)
            flow  = InstalledAppFlow.from_client_secrets_file(str(client_path), DRIVE_SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json())
        print(f"  Saved OAuth token → {token_path}")

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def upload_pdf(svc, entry_name: str, pdf_path: Path) -> str:
    """Upload pdf_path to the fixtures folder and return the Drive file ID."""
    from googleapiclient.http import MediaFileUpload

    dest_name = f"fixture_{entry_name}.pdf"
    existing = svc.files().list(
        q=f"name='{dest_name}' and '{FIXTURES_FOLDER_ID}' in parents and trashed=false",
        fields="files(id,name)"
    ).execute().get("files", [])
    for f in existing:
        svc.files().delete(fileId=f["id"]).execute()
        print(f"  Replaced existing {dest_name} (id={f['id']})")

    media = MediaFileUpload(str(pdf_path), mimetype="application/pdf", resumable=False)
    meta  = {"name": dest_name, "parents": [FIXTURES_FOLDER_ID]}
    result = svc.files().create(body=meta, media_body=media, fields="id").execute()
    return result["id"]


def update_manifest(updates: list[tuple[str, str, str]], strip_local: bool = False):
    """
    Update fixtures_manifest.json in-place.
    updates: list of (entry_name, format, file_id)

    For each update:
      - If an entry with matching name exists, update its file_id
      - If an entry with matching format and REPLACE_ME file_id exists, update it
      - Otherwise append a new entry
    When strip_local is True, also remove any `source`/`path` keys from a matched
    entry so it reads from Drive instead of the local clients dir (used when
    migrating source='repo' fixtures to Drive).
    Also updates Bookkeeping-clients/fixtures_manifest.json if BOOKKEEPING_CLIENTS_DIR is set.
    """
    paths = []
    if MANIFEST_PATH.exists():
        paths.append(MANIFEST_PATH)
    clients_manifest = _clients_dir() / "fixtures_manifest.json"
    if clients_manifest.exists() and clients_manifest not in paths:
        paths.append(clients_manifest)

    for path in paths:
        with open(path) as f:
            manifest = json.load(f)

        stmts = manifest.get("statements", [])

        for entry_name, fmt, file_id in updates:
            # Try to find by entry name first
            matched = False
            for entry in stmts:
                if entry.get("name") == entry_name:
                    entry["file_id"] = file_id
                    entry["format"]  = fmt
                    if strip_local:
                        entry.pop("source", None)
                        entry.pop("path", None)
                    matched = True
                    break

            if not matched:
                # Try to find a REPLACE_ME placeholder for this format
                for entry in stmts:
                    if entry.get("format") == fmt and entry.get("file_id") == "REPLACE_ME":
                        entry["name"]    = entry_name
                        entry["file_id"] = file_id
                        matched = True
                        break

            if not matched:
                # Append new entry
                stmts.append({
                    "name":           entry_name,
                    "file_id":        file_id,
                    "format":         fmt,
                    "expect_client":  None,
                    "statement_date": None,
                })

        manifest["statements"] = stmts
        with open(path, "w") as f:
            json.dump(manifest, f, indent=2)
            f.write("\n")
        print(f"  Updated {path}")


def _resolve_repo_fixtures():
    """Find every source='repo' fixture and resolve its local PDF path.

    Returns a list of (entry_name, format, local_path). Reads the clients-dir
    manifest if present (the source of truth), else the repo manifest.
    """
    clients_dir = _clients_dir()
    manifest_path = clients_dir / "fixtures_manifest.json"
    if not manifest_path.exists():
        manifest_path = MANIFEST_PATH
    if not manifest_path.exists():
        print(f"ERROR: no fixtures_manifest.json found in {clients_dir} or {MANIFEST_PATH.parent}")
        sys.exit(1)
    with open(manifest_path) as f:
        manifest = json.load(f)

    out = []
    for entry in manifest.get("statements", []):
        if entry.get("source") != "repo":
            continue
        rel   = entry.get("path", f"fixtures/fixture_{entry['name']}.pdf")
        local = clients_dir / rel
        if not local.exists():
            print(f"  ⚠ skip {entry['name']}: local PDF not found at {local}")
            continue
        out.append((entry["name"], entry.get("format"), local))
    return out


def migrate_repo_fixtures():
    """Upload every source='repo' fixture to Drive and rewrite its manifest
    entry to read from Drive (file_id), dropping the local source/path."""
    repo_fixtures = _resolve_repo_fixtures()
    if not repo_fixtures:
        print("No source='repo' fixtures to migrate — all fixtures already on Drive. ✓")
        return

    print(f"Found {len(repo_fixtures)} local fixture(s) to migrate:")
    for name, fmt, path in repo_fixtures:
        print(f"  • {name} ({fmt}) ← {path}")

    print("\nConnecting to Drive...")
    svc = _get_service()

    updates = []
    for name, fmt, path in repo_fixtures:
        print(f"\nUploading {path.name} as '{name}' ({fmt})...")
        file_id = upload_pdf(svc, name, path)
        updates.append((name, fmt, file_id))
        print(f"  ✓ Uploaded → file_id={file_id}")

    print("\nUpdating fixtures_manifest.json (stripping local source/path)...")
    update_manifest(updates, strip_local=True)

    print("\n✅ Migrated. The PDFs now live in Drive; the local copies are no longer")
    print("   referenced by the manifest. Commit the manifest, then delete the local PDFs:")
    print("   cd $BOOKKEEPING_CLIENTS_DIR && git add fixtures_manifest.json && git commit -m 'Migrate fixtures to Drive' && git push")


def main():
    args = sys.argv[1:]

    if args and args[0] == "--migrate-repo":
        migrate_repo_fixtures()
        return

    if not args or len(args) % 3 != 0:
        print(__doc__)
        sys.exit(1)

    triples = [(args[i], args[i + 1], Path(args[i + 2])) for i in range(0, len(args), 3)]

    for entry_name, fmt, pdf_path in triples:
        if fmt not in VALID_FORMATS:
            print(f"ERROR: Unknown format '{fmt}'. Valid formats: {sorted(VALID_FORMATS)}")
            sys.exit(1)
        if not pdf_path.exists():
            print(f"ERROR: File not found: {pdf_path}")
            sys.exit(1)

    print("Connecting to Drive...")
    svc = _get_service()

    updates = []
    for entry_name, fmt, pdf_path in triples:
        print(f"\nUploading {pdf_path.name} as '{entry_name}' ({fmt})...")
        file_id = upload_pdf(svc, entry_name, pdf_path)
        updates.append((entry_name, fmt, file_id))
        print(f"  ✓ Uploaded → file_id={file_id}")

    print("\nUpdating fixtures_manifest.json...")
    update_manifest(updates)

    print("\n✅ Done. Commit the updated fixtures_manifest.json in Bookkeeping-clients:")
    print("   cd $BOOKKEEPING_CLIENTS_DIR && git add fixtures_manifest.json && git commit -m 'Add fixture PDFs' && git push")


if __name__ == "__main__":
    main()
