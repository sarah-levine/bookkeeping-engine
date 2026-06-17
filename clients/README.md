# Client configs

Each client of the reconciliation + payroll engine is described by one JSON
file in a *clients directory*. **Real client files are not kept in this
repository** — they contain vendor names, employee names, bank accounts, and
pay-split rules. They live in a separate private location.

This folder ships only:

- **`_schema.json`** — JSON Schema documenting the expected structure.
- **`example_client.json`** — a fake "Acme Inc" config showing how to fill
  one in.

## Where the engine looks for client configs

`get_clients_dir()` (in `log_utils.py`) resolves the clients directory in this
priority order:

1. **`BOOKKEEPING_CLIENTS_DIR`** environment variable, if set and the path
   exists.
2. **`~/.bookkeeping/clients/`**, if it exists.
3. **`./clients/`** (this folder) as a fallback.

If the env var points at a missing path, the engine warns and falls back to
the defaults above.

## Add your own clients

Pick one of the two private locations so your real data stays out of this
repo:

```bash
# Option A — well-known path (no env var needed)
mkdir -p ~/.bookkeeping/clients
cp example_client.json ~/.bookkeeping/clients/your_client.json   # then edit

# Option B — anywhere + env var
export BOOKKEEPING_CLIENTS_DIR=/path/to/your/private/clients
```

Then create one JSON file per client (start from `example_client.json`).
Reconciliation auto-detects and loads every `*.json` it finds in the resolved
directory. No code changes are needed to add a client.

See `../ADDING_NEW_CLIENT.md` for a field-by-field walkthrough.

## Shared private config files

Besides the per-client `*.json` files above, a few **shared** config files
also live in the resolved clients directory. They hold client names, account
lists, sheet IDs, and email addresses, so they are kept out of the public repo
the same way. Each one ships a committed `*.example.json` template in the repo
root; copy it into your clients directory and fill in real values:

| File | Used by | Holds |
|------|---------|-------|
| `digest_config.json` | `send_morning_digest.py` | Tracker layout, client/account display names, CC blocking rules, fallback dates, and the digest email addresses + sheet URL. |
| `sheets_config.json` | `sheets_updater.py` | Spreadsheet ID, cell map, and client/account label tables for the Google Sheet tracker. |
| `manual_statements.json` | `manual_statement_entry.py` | Manually-keyed statement balances for accounts with no downloadable PDF. |

These are loaded by `load_private_json(name, default)` in `log_utils.py`, which
looks in the clients directory first, then a repo-root `*.example.json`, then
the supplied default — so the engine still runs (with fake Acme data) when no
private file is present.

### `digest_config.json` structure

Start from `../digest_config.example.json`. Top-level keys:

- **`email`** — `sender`, `recipient`, `cc_recipient`, and `sheet_url` for the
  morning digest. `cc_recipient` is only CC'd on scheduled (`--scheduled`) runs.
- **`client_display_names`** — maps raw client-name variants (lowercased) from
  the logs to a short display name, e.g. `"acme inc llc": "Acme"`.
- **`account_display_names`** — maps raw `account_type` keys to display labels,
  e.g. `"bofa_checking": "BofA Checking"`.
- **`tracker`** — ordered list of clients shown in the tracker grid. Each entry
  has a `client` display name, a `client_keys` list (every log key/alias that
  maps to this client), and an `accounts` list. Each account has a `label`,
  a `key` (the `account_type`), an optional `fallback_date` (MM/DD/YY — a floor
  used until the live log or sheet has a newer date), and an optional
  `client_provided: true` for accounts you wait on the client to send.
- **`cc_blocking_rules`** — keyed by client display name; drives the "waiting on
  CC" highlighting. Each rule lists the `blocked` checking/savings keys, the
  `payroll_blocked` keys, the `checking_key`, and a `cc_blockers` list of
  `{key, closing_day}` credit cards that must be reconciled first.

## A note on `.gitignore`

This repo's `.gitignore` ignores `clients/*.json` (with exceptions for
`example_client.json` and `_schema.json`) so real client files can't be
committed here by accident. The shared configs above are ignored by name in
the repo root too (`digest_config.json`, `sheets_config.json`,
`manual_statements.json`) — only their `*.example.json` templates are
committed.
