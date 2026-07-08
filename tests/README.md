# Test suite

| File | Needs fixtures? | What it covers |
|------|-----------------|----------------|
| `test_log_pipeline.py`     | no  | `payroll_log → reconciliation_log → tracker` write/read/render (synthetic) |
| `test_config_and_logs.py`  | no  | `get_logs_dir()` resolution, schema validation, registry skips non-dict JSON, payroll dispatch |
| `test_aggregations.py`     | no  | config-driven transaction roll-ups (`parsers.*.aggregate_transactions`), synthetic transactions |
| `test_vendor_normalize.py` | no  | standard + client-tier vendor normalization, no duplicate copies |
| `test_bmo_credit.py`       | no  | `BMOCreditCardParser` parsing/report generation against synthetic text |
| `test_statement_registry.py` | no | `parsers/registry.py`: schema/registry key parity, adding a new format needs only one `register()` call |
| `test_drive_archiver_credentials.py` | no | `drive_archiver._get_service()`'s credential fallback chain (mocked, no real creds/network) |
| `test_mark_payroll_done.py` | no | `mark_payroll_done.py` CLI: payroll_key/name resolution, upsert semantics, error handling |
| `test_bofa_check_payees.py` | no | BofA checking's check-image payee OCR + opt-in Claude Vision extraction (mocked) |
| `test_vision_helper_check_payees.py` | no | `extractors.vision_helper.extract_check_payees()` in isolation (mocked) |
| `test_cc_payment_classification.py` | no | shared card-network-payment fallback classifier used by `bofa.py`/`amex.py` |
| `test_citi_costco_closing_date.py` | no | `CitiVisaCostcoParser` closing-date extraction, including real OCR-garbled statement text |
| `test_citi_amount_types.py` | no | `CitiCheckingParser`/`CitiVisaCostcoParser` store transaction amounts as `Decimal`, not `str` |
| `test_chase_balance_check.py` | no | `ChaseParser` prints an explicit balance-verification line |
| `test_adp_payroll_details_earnings.py` | no | `adp_payroll_details.py`'s generic earnings-category fallback (unknown categories included, not dropped) |
| `test_adp_multi_journal_wiring.py` | no | `adp_payroll_departments`/`adp_labor_distribution`'s monkeypatch-and-capture test wiring |
| `test_square_payroll.py`   | no | `square_payroll.py`'s Company Totals xlsx parsing (aggregate-block-only, tax-category bucketing, multi-day-range/unhandled-deduction guards) and journal balance |
| `test_report_helper_imports.py` | no | every parser actually resolves the `parsers.report` helpers it calls — guards against `from parsers.report import *` silently dropping underscore-prefixed names (see note below) |
| `test_parsers.py`          | yes | each bank parser extracts balances/line items from a real statement |
| `test_payroll.py`          | yes | each ADP payroll format parses with a balance tie-out (parse only — no `_build_journal`, no log writes) |
| `test_end_to_end.py`       | yes | bank pipeline: PDF → `detect_statement_type` → parser → report → `write_both_logs` → digest read |
| `test_payroll_end_to_end.py` | yes | payroll pipeline: PDF → real parse chain → real journal builder → balance check → `append_payroll_log()` → read back, plus a check that `update_sheet()` is actually called |

Everything above the fixture-backed group runs anywhere, including CI, with
no PDFs or credentials — parsers are instantiated via `__new__` (bypassing
PDF extraction) and fed synthetic or hand-built real-world text, and any
external call (Vision, Drive, GitHub) is mocked. The fixture-backed ones
**skip** when fixtures/credentials are absent, so a fresh public checkout
stays green.

Run everything:
```bash
python3 -m pytest tests/ -v
```

**`from parsers.report import *` gotcha:** `parsers/report.py` defines no
`__all__`, and every one of its section helpers (`_report_header`,
`_balance_check`, `_deposits_section`, etc.) is underscore-prefixed —
Python's `import *` silently excludes underscore names in that case. A
parser that relies on `import *` alone for these gets none of them, and
`generate_report()` raises `NameError` the first time it actually runs
(found live in `northern_trust.py` and `bmo.py` — both had zero test
coverage of that code path until a missing dependency, see below, was
installed). If you add a new parser, either import the specific helpers
you need explicitly (`from parsers.report import _report_header, ...`,
the pattern most parsers already use) or rely on
`test_report_helper_imports.py` to catch the gap.

**OCR/`pytesseract` can be silently missing.** Northern Trust and BMO
statements require `fitz` + `pytesseract` (no `pdftotext` fallback for
Northern Trust) — if `pytesseract` isn't installed, their tests just
**skip**, with no error and no obvious signal that a whole parser's test
coverage has quietly dropped to zero. Check
`python3 -c "import pytesseract"` before trusting a green test run covers
these two parsers; installing it (the `tesseract` binary is often already
present via Homebrew — only the Python wrapper is usually missing) is what
surfaced two real bugs in this codebase that had gone undetected.

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

5. **Payroll fixtures (`test_payroll_end_to_end.py`) are local-only** — no
   Drive download, just files already in the private clients repo. Copy the
   example and fill in real filenames:
   ```bash
   cp tests/payroll_fixtures_manifest.example.json tests/payroll_fixtures_manifest.json
   # edit tests/payroll_fixtures_manifest.json — real pdf/config filenames from
   # <clients_dir>/fixtures/ and <clients_dir>/ in the private repo
   ```
   `payroll_fixtures_manifest.json` is gitignored — the real client/fixture
   names it contains must never land in the tracked `.py` file or `.example.json`.

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

