"""
test_drive_archiver_credentials.py
------------------------------------
Regression test for drive_archiver._get_service()'s credential fallback
chain.

Previously, after successfully building service-account credentials
(source 3: GOOGLE_SHEETS_CREDENTIALS / sheets_credentials.json), the next
check was `if not creds or not creds.valid`. A freshly-constructed
service_account.Credentials object always reports valid=False until its
first actual API request (google-auth fetches the token lazily), so this
always fell through to source 4 (interactive OAuth) and raised
"No Drive credentials found" even though usable credentials existed —
effectively making the service-account fallback dead code.

No real Google credentials or network access needed: from_service_account_info
and build() are both mocked.
"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import drive_archiver


class _FakeServiceAccountCreds:
    """Mirrors a real service_account.Credentials object immediately after
    construction: valid=False until first use, no expiry/refresh_token."""
    def __init__(self):
        self.valid = False
        self.expired = False
        self.refresh_token = None


class GetServiceCredentialFallbackTest(unittest.TestCase):
    def setUp(self):
        self.clients_dir = Path(tempfile.mkdtemp())
        (self.clients_dir / "sheets_credentials.json").write_text(
            json.dumps({"type": "service_account"})
        )
        self._saved_env = {
            k: os.environ.get(k)
            for k in ("BOOKKEEPING_CLIENTS_DIR", "DRIVE_TOKEN_B64", "GOOGLE_SHEETS_CREDENTIALS")
        }
        os.environ["BOOKKEEPING_CLIENTS_DIR"] = str(self.clients_dir)
        os.environ.pop("DRIVE_TOKEN_B64", None)
        os.environ.pop("GOOGLE_SHEETS_CREDENTIALS", None)

    def tearDown(self):
        shutil.rmtree(self.clients_dir)
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    @patch("googleapiclient.discovery.build")
    @patch("google.oauth2.service_account.Credentials.from_service_account_info")
    def test_service_account_credentials_used_without_falling_through_to_oauth(
        self, mock_from_info, mock_build
    ):
        # Only source 3 is available in this test's clients_dir — no
        # drive_token.pickle, no DRIVE_TOKEN_B64, no drive_credentials.json.
        fake_creds = _FakeServiceAccountCreds()
        mock_from_info.return_value = fake_creds
        mock_build.return_value = "the-drive-service"

        result = drive_archiver._get_service()

        self.assertEqual(result, "the-drive-service")
        mock_build.assert_called_once_with("drive", "v3", credentials=fake_creds)

    def test_no_credentials_anywhere_raises_clear_error(self):
        # With no sheets_credentials.json either, _get_service() should still
        # raise its explanatory EnvironmentError, not something more obscure.
        (self.clients_dir / "sheets_credentials.json").unlink()
        with self.assertRaises(EnvironmentError):
            drive_archiver._get_service()


if __name__ == "__main__":
    unittest.main(verbosity=2)
