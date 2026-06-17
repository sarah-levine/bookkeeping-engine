# Claude Instructions — sarah-levine/Bookkeeping

## Patch fixes
Whenever applying a data patch or workaround (e.g. manually correcting a CSV entry, backfilling a date, removing a bad log record), always:
1. Identify the root cause before patching
2. Either fix the root cause in the same session, or add it to `REFACTORING_ROADMAP.md` under **Open: Needs Root Cause Fix** before committing the patch
3. Never ship a patch-only fix without one of the above
4. When fixing a parser or classification bug, always scan existing log files (`recon_log.json`, `reconciliation_log.csv`) for historical entries affected by the same bug and correct them in the same commit

## Client name governance
Never write a new client key, name variant, or account_type to any log file (`recon_log.json`, `reconciliation_log.csv`, `payroll_log.csv`) without explicit user confirmation. If a reconciliation or payroll run surfaces an unrecognized client name or account type, stop and ask before committing. The runtime guards `log_utils._assert_known_client` and `log_utils._assert_known_account_type` enforce this technically — do not bypass them.

## Branch hygiene
Always maintain exactly one active feature branch alongside `main`. Rules:
1. Before starting new work, check `git branch -a` — if a stale merged branch exists, delete it first
2. After merging to `main`, immediately delete the feature branch (`git push origin --delete <branch>`)
3. Never let two feature branches exist simultaneously
4. Keep the working branch rebased on `main`; resolve conflicts before they accumulate

## Public-repo hygiene (no real client data in code)
This repo is published (or will be); real client data lives only in the private
`Bookkeeping-clients` repo. Code, comments, docstrings, and example/JSON files
must never contain real client, person, or counterparty names, account/card
numbers, or non-generic emails.

Rules:
1. Docstring/comment transaction examples must use fictional stand-ins — the
   adopted placeholders (Acme/Bravo/Charlie…, Jane Doe, John Roe) or the classic
   fictional companies (Contoso, Fabrikam). Never paste a real statement line.
2. A leak tripwire runs in CI and as a pre-commit hook: `tools/pii_scan.py`
   (allowlist-based — flags any proper name, account-number pattern, or
   non-approved email not in `tools/pii_allowlist.txt`). Run
   `python3 tools/pii_scan.py --audit` before any publish for a max-recall sweep.
3. Install the hook once: `git config core.hooksPath tools/hooks`.
4. If the scanner flags something genuinely generic/fictional, add it to
   `tools/pii_allowlist.txt` (a deliberate, reviewable decision). If it flags
   something real, scrub it — do not allowlist it.
