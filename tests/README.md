# Test suite

| File | Needs fixtures? | What it covers |
|------|-----------------|----------------|
| `test_log_pipeline.py`     | no  | `payroll_log → reconciliation_log → tracker` write/read/render (synthetic) |
| `test_config_and_logs.py`  | no  | `get_logs_dir()` resolution, schema validation, registry skips non-dict JSON, payroll dispatch |
| `test_parsers.py`          | yes | each bank parser extracts balances/line items from a real statement |
| `test_payroll.py`          | yes | each ADP payroll format parses with a balance tie-out |
| `test_end_to_end.py`       | yes | full pipeline: PDF → `detect_statement_type` → parser → report → `write_both_logs` → digest read |

The first two run anywhere (including CI). The fixture-backed three **skip**
when fixtures/credentials are absent, so a fresh public checkout stays green.

Run everything:
```bash
python3 -m pytest tests/ -v
```

## Parser/payroll/e2e fixtures (Google Drive or local)

These tests run parsers against a **real PDF** that never lives in this repo —
either pulled from Google Drive by file ID, or read from the private clients
dir (`source: "repo"` in the manifest). At runtime the harness loads each
fixture, runs the matching parser, and checks it produced sensible output.

If credentials or a configured manifest are missing, the tests **skip**
instead of failing — a fresh public checkout with no secrets stays green.

## One-time setup

1. **Enable the Drive API** in the GCP project (same project as the Sheets
   integration, `bookkeeping-498118`):
   https://console.developers.google.com/apis/api/drive.googleapis.com/overview?project=356280472722
   → click **Enable**.

2. **Create a Drive folder** for fixtures, e.g. `Bookkeeping Test Fixtures`,
   and drop in **one representative PDF per format** (one BofA checking, one
   Citi Costco, one ADP payroll, etc.).

3. **Share the folder with the service account.** Open the service-account
   JSON and copy its `client_email` (looks like
   `something@example-project.iam.gserviceaccount.com`). In Drive, share
   the fixtures folder with that email (Viewer is enough).

4. **Build the manifest.** Copy the example and fill in the Drive file IDs
   (the part after `/d/` in each file's share URL):
   ```bash
   cp tests/fixtures_manifest.example.json tests/fixtures_manifest.json
   # edit tests/fixtures_manifest.json — replace each REPLACE_ME with a real file_id
   ```
   `fixtures_manifest.json` and the download cache are gitignored.

## Running

```bash
# credentials come from the same env var the Sheets updater uses
export GOOGLE_SHEETS_CREDENTIALS="$(cat ~/Downloads/bookkeeping-498118-xxxxx.json)"

# with pytest
python3 -m pytest tests/test_parsers.py -v

# or as a plain script
python3 tests/test_parsers.py
```

## What it checks (today)

Smoke level: each parser runs without error and produces **either**
transactions **or** balances; if `expect_client` is set in the manifest, the
detected client name must match. Downloaded PDFs are cached in
`tests/.fixture_cache/` so reruns don't re-download.

## Extending

- Set `expect_client` on a fixture to assert client auto-detection.
- Add golden-value checks (exact transaction counts / totals) once you've
  confirmed a fixture's correct output.
- Payroll-format fixtures can be added with their own manifest section and a
  runner that calls the journal builders in `payroll_clients/`.

## Recommended workflow: grow fixtures over time

You don't need to assemble every format up front. The low-effort path:

1. Keep all fixtures under one Drive folder tree (the `Bookkeeping` folder,
   which already holds the per-client `Bank Statements` subfolders). **Share
   that single parent folder** with the service account once — everything
   underneath is then readable, including anything you add later.
2. Start with whatever statements you already have in Drive — one working
   fixture proves the harness.
3. Each month as you reconcile, that month's PDF is already in hand. Drop it
   into the fixtures area and add one line to `fixtures_manifest.json`. Within
   a normal monthly cycle you accumulate full coverage with no dedicated
   gathering session.

