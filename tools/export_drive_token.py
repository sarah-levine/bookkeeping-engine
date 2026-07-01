"""
export_drive_token.py
---------------------
Serializes drive_token.pickle to a base64 string suitable for setting as the
DRIVE_TOKEN_B64 environment variable in sandboxed / remote sessions that
have no access to the local filesystem.

Usage:
    python3 tools/export_drive_token.py

Then in the sandboxed session set:
    export DRIVE_TOKEN_B64="<printed string>"

The token is valid for ~1 hour and auto-refreshes if a refresh_token is
present. Re-run this script after the token expires to get a fresh value.
"""

import base64
import os
import pickle
from pathlib import Path


def main():
    clients_dir = Path(
        os.environ.get("BOOKKEEPING_CLIENTS_DIR")
        or Path.home() / ".bookkeeping" / "clients"
    )
    token_path = clients_dir / "drive_token.pickle"

    if not token_path.exists():
        print(f"ERROR: drive_token.pickle not found at {token_path}")
        print("Run reconcile_comprehensive.py once locally to generate it.")
        return

    with open(token_path, "rb") as f:
        creds = pickle.load(f)

    print(f"Token valid:   {creds.valid}")
    print(f"Token expiry:  {creds.expiry}")
    print(f"Has refresh:   {bool(getattr(creds, 'refresh_token', None))}")
    print()

    b64 = base64.b64encode(pickle.dumps(creds)).decode()
    print("Set this in your sandboxed session:")
    print()
    print(f"export DRIVE_TOKEN_B64=\"{b64}\"")


if __name__ == "__main__":
    main()
