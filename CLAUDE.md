# Claude Instructions — sarah-levine/Bookkeeping

## Patch fixes
Whenever applying a data patch or workaround (e.g. manually correcting a CSV entry, backfilling a date, removing a bad log record), always:
1. Identify the root cause before patching
2. Either fix the root cause in the same session, or add it to `REFACTORING_ROADMAP.md` under **Open: Needs Root Cause Fix** before committing the patch
3. Never ship a patch-only fix without one of the above
4. When fixing a parser or classification bug, always scan existing log files (`recon_log.json`, `reconciliation_log.csv`) for historical entries affected by the same bug and correct them in the same commit

## Status value renames
Whenever a status string value is renamed (e.g. `CLEAN` → `DONE`), always backfill existing log entries in the same session:
1. Run `python3 tools/backfill_status.py <old> <new>` against the private logs dir
2. Commit the updated `recon_log.json` to `Bookkeeping-clients` in the same commit as the code change

## Client name governance
Never write a new client key, name variant, or account_type to any log file (`recon_log.json`, `reconciliation_log.csv`, `payroll_log.csv`) without explicit user confirmation. If a reconciliation or payroll run surfaces an unrecognized client name or account type, stop and ask before committing. The runtime guards `log_utils._assert_known_client` and `log_utils._assert_known_account_type` enforce this technically — do not bypass them.

## Branch hygiene
Always maintain exactly one active feature branch alongside `main`. Rules:
1. Before starting new work, check `git branch -a` — if a stale merged branch exists, delete it first
2. After merging to `main`, immediately delete the feature branch (`git push origin --delete <branch>`)
3. Never let two feature branches exist simultaneously
4. Keep the working branch rebased on `main`; resolve conflicts before they accumulate

## Always paste the raw report/journal output — never paraphrase it
This has been forgotten repeatedly (recurring user complaint, not a one-off):
after running `reconcile_comprehensive.py` or `payroll.py`, the reply to the
user replaced the tool's own printed report with a hand-written bullet-point
summary instead.

The rule: whenever `reconcile_comprehensive.py` or `payroll.py` produces a
report/journal-entry table relevant to what the user asked for, that exact
text — the `====`-delimited STATEMENT SUMMARY / CREDITS / WITHDRAWALS /
CHECKS / PAYROLL / CREDIT CARD PAYMENTS sections, or the full payroll journal
entry table — goes into the reply **verbatim**, in a code block, not
re-derived into prose or a hand-built table. A short summary sentence may
accompany it, but must never *replace* it. If a run happened and its output
isn't in the reply, that's the bug this rule exists to catch — go back and
paste it before sending the reply.

This is not "when the user asks to see it" — it's the default for every run
whose result the user needs to review or act on (i.e. essentially every
reconcile/payroll run in this workflow).

## Testing policy: real fixtures over synthetic data
Synthetic/hand-built test data has repeatedly passed while the real-world
equivalent broke — e.g. a payroll regex that only failed on the trailing
tax-column text `pdftotext` produces from a real ADP export, never
reproduced by a clean hand-built test line; two parsers where
`generate_report()` raised `NameError` on real OCR output but no synthetic
test ever exercised that code path. A green synthetic-only test suite is
not proof a fix works.

Rule: whenever a change touches a parser (or anything that consumes its
output), verify it against **every real fixture that parser has** — Drive
or the private `Bookkeeping-clients` repo — with a before/after diff of the
full printed report, not just 1–2 samples and not synthetic data alone.
Synthetic tests still matter (they're what keeps CI green without secrets,
and they're the only option when no real fixture exists yet) — but treat
them as coverage-for-when-real-data-is-unavailable, not a substitute for
real-fixture verification when a fixture does exist.

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
