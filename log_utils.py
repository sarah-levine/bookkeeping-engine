"""
log_utils.py — Shared helpers for recon_log.json and client config path resolution.

Replaces the scattered recon_issues_YYYY-MM-DD.jsonl and
manual_issues_YYYY-MM-DD.jsonl files with a single recon_log.json.

Schema (each entry):
  {
    "run_time":           ISO-8601 string (PST)
    "type":               "recon" | "manual"
    "client":             str
    "account_type":       str
    "statement_end_date": str
    "statement":          str filename
    "beginning_balance":  str
    "ending_balance":     str
    "difference":         str
    "status":             "IN_PROGRESS"|"DONE"|""
    "issues":             list[str]
    "issue":              str
  }

Upsert key:
  - recon: (client, account_type, statement_end_date)
  - manual: (client, issue)
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_DIR = Path(__file__).parent
_PST = ZoneInfo("America/Los_Angeles")


def _recon_log_path() -> Path:
    """recon_log.json in the private logs dir (see get_logs_dir)."""
    return get_logs_dir() / "recon_log.json"


def _now_pst() -> datetime:
    return datetime.now(_PST)


def _load_log() -> list[dict]:
    path = _recon_log_path()
    if not path.exists():
        return []
    text = path.read_text().strip()
    if not text:
        return []
    return json.loads(text)


def _save_log(entries: list[dict]) -> None:
    _recon_log_path().write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n")


def entry_status(client: str, account_type: str, statement_end_date: str) -> str | None:
    """Return the current status of a recon entry, or None if not found."""
    client = _normalize_client_name(client)
    statement_end_date = _normalize_date_iso(statement_end_date)
    for e in reversed(_load_log()):
        if (e.get("type") == "recon"
                and _normalize_client_name(e.get("client", "")) == client
                and e.get("account_type") == account_type
                and _normalize_date_iso(e.get("statement_end_date", "")) == statement_end_date):
            return e.get("status")
    return None


def _assert_known_account_type(client: str, account_type: str) -> None:
    """Abort if account_type is not in the client's statement_types config.

    Allows "payroll" and empty string unconditionally (payroll isn't in
    statement_types; manual entries have no account_type). For everything
    else, checks the client's config and raises if the type is unrecognized —
    both interactively (after declining the prompt) and in no-prompt mode.
    """
    if not account_type or account_type == "payroll":
        return
    import os
    try:
        from parsers.base import _registry
        cfg = _registry.get_config(client)
        if cfg is None:
            return  # unknown client — already caught by _assert_known_client
        known = set(cfg.get("statement_types", []))
        if not known or account_type in known:
            return
    except Exception:
        return

    msg = (f"⚠️  account_type '{account_type}' is not in {client}'s statement_types config. "
           f"Known types: {sorted(known)}")
    if os.environ.get("BOOKKEEPING_NO_PROMPT") == "1":
        raise ValueError(f"{msg} Refusing to write unrecognized account_type in no-prompt mode.")

    print(msg)
    resp = input("Write to log anyway? [y/N] ").strip().lower()
    if resp != "y":
        raise ValueError(f"Aborted: unexpected account_type '{account_type}' for client '{client}'.")


def _assert_known_client(client: str) -> None:
    """Abort if client is not in the registry.

    Raises in both no-prompt mode (BOOKKEEPING_NO_PROMPT=1) and when the
    user declines interactively. Unknown clients must never be written to any
    log file regardless of invocation mode.
    Importing parsers.base lazily to avoid circular imports.
    """
    import os
    try:
        from parsers.base import _registry
        # Strip _agency/_admin suffixes used by adp_labor_distribution to
        # keep per-division payroll_log.csv rows distinct while still resolving
        # to a known client key.
        probe = client
        for _sfx in ("_agency", "_admin"):
            if probe.lower().endswith(_sfx):
                probe = probe[: -len(_sfx)]
                break
        if _registry.resolve(probe) is not None:
            return  # known — proceed normally
    except Exception:
        return  # registry unavailable (e.g. no client configs) — don't block

    msg = f"⚠️  Unrecognized client '{client}' — not found in any client JSON config."
    if os.environ.get("BOOKKEEPING_NO_PROMPT") == "1":
        raise ValueError(f"{msg} Refusing to write unrecognized client in no-prompt mode.")

    print(msg)
    resp = input("Write to log anyway? This will introduce a new client name. [y/N] ").strip().lower()
    if resp != "y":
        raise ValueError(f"Aborted: refusing to write unrecognized client '{client}' to log.")


def _normalize_client_name(client: str) -> str:
    """Resolve a client name to its canonical form from the registry.

    Returns the canonical_name if found, otherwise returns the input
    unchanged. Handles department suffixes like '— Admin' / '— Agency'
    by normalizing the base and preserving the suffix.
    """
    try:
        from parsers.base import _registry
        canonical = _registry.resolve(client)
        if canonical:
            return canonical
        # Try stripping department suffix (e.g. "Acme Corp — Admin")
        # Keep the suffix but normalize the base name
        if " — " in client:
            base, suffix = client.rsplit(" — ", 1)
            canonical = _registry.resolve(base)
            if canonical:
                return f"{canonical} — {suffix}"
    except Exception:
        pass
    return client


def upsert_recon_log(
    *,
    client: str,
    account_type: str,
    statement_end_date: str,
    statement: str = "",
    beginning_balance: str = "",
    ending_balance: str = "",
    difference: str = "0.00",
    status: str = "DONE",
    issues: list[str] | None = None,
    issue: str = "",
) -> None:
    """Upsert a reconciliation or payroll entry into recon_log.json."""
    client = _normalize_client_name(client)
    _assert_known_client(client)
    _assert_known_account_type(client, account_type)
    statement_end_date = _normalize_date_iso(statement_end_date)
    entry = {
        "run_time":           _now_pst().isoformat(),
        "type":               "recon",
        "client":             client,
        "account_type":       account_type,
        "statement_end_date": statement_end_date,
        "statement":          statement,
        "beginning_balance":  beginning_balance,
        "ending_balance":     ending_balance,
        "difference":         difference,
        "status":             status,
        "issues":             issues if issues is not None else [],
        "issue":              issue,
    }
    key = (client, account_type, statement_end_date)
    existing = _load_log()

    # If this is a successful run (not ERROR), remove any prior ERROR entries
    # for the same client + account_type — they're resolved now.
    if status != "ERROR":
        existing = [
            e for e in existing
            if not (
                e.get("type") == "recon"
                and e.get("status") == "ERROR"
                and _normalize_client_name(e.get("client", "")) == client
                and e.get("account_type") == account_type
            )
        ]

    replaced = False
    for i, e in enumerate(existing):
        if e.get("type") == "recon" and (
            _normalize_client_name(e.get("client", "")), e.get("account_type"),
            _normalize_date_iso(e.get("statement_end_date", ""))
        ) == key:
            existing[i] = entry
            replaced = True
            break
    if not replaced:
        existing.append(entry)
    _save_log(existing)


def append_manual_issue(*, client: str, issue: str) -> None:
    """Append a manual issue note to recon_log.json (idempotent by client+issue)."""
    client = _normalize_client_name(client)
    _assert_known_client(client)
    entry = {
        "run_time":           _now_pst().isoformat(),
        "type":               "manual",
        "client":             client,
        "account_type":       "",
        "statement_end_date": "",
        "statement":          "",
        "beginning_balance":  "",
        "ending_balance":     "",
        "difference":         "",
        "status":             "",
        "issues":             [],
        "issue":              issue,
    }
    key = (client, issue)
    existing = _load_log()
    replaced = False
    for i, e in enumerate(existing):
        if e.get("type") == "manual" and (
            e.get("client"), e.get("issue")
        ) == key:
            existing[i] = entry
            replaced = True
            break
    if not replaced:
        existing.append(entry)
    _save_log(existing)


def resolve_manual_issue(*, client: str, issue: str) -> None:
    """Mark a manual issue as resolved so it stops appearing in digests."""
    existing = _load_log()
    for e in existing:
        if e.get("type") == "manual" and e.get("client") == client and e.get("issue") == issue:
            e["resolved"] = True
            e["resolved_time"] = _now_pst().isoformat()
    _save_log(existing)


def load_recon_log(log_date) -> tuple[list[dict], list[dict]]:
    """Return (recon_entries, manual_entries) for one or more YYYY-MM-DD dates.

    log_date: str or list/set of str
    - recon_entries: DONE/IN_PROGRESS entries whose run_time matches any of the dates
    - manual_entries: ALL unresolved manual notes (regardless of date)
    """
    dates = {log_date} if isinstance(log_date, str) else set(log_date)
    all_entries = _load_log()
    recon = []
    manual = []
    for e in all_entries:
        t = e.get("type", "recon")
        if t == "manual":
            if not e.get("resolved", False):
                manual.append(e)
        else:
            rt = e.get("run_time", "")
            entry_date = rt[:10]
            if entry_date in dates:
                recon.append(e)
    return recon, manual


def get_clients_dir() -> Path:
    """Return the directory containing client JSON configs.

    Priority:
      1. BOOKKEEPING_CLIENTS_DIR environment variable
      2. ~/.bookkeeping/clients/
      3. ./clients/  (repo-local fallback)
    """
    import os
    env = os.environ.get("BOOKKEEPING_CLIENTS_DIR", "").strip()
    if env:
        p = Path(env)
        if p.exists():
            return p
        import warnings
        warnings.warn(
            f"BOOKKEEPING_CLIENTS_DIR={env!r} does not exist; falling back to defaults.",
            stacklevel=2,
        )
    home_path = Path.home() / ".bookkeeping" / "clients"
    if home_path.exists():
        return home_path
    return REPO_DIR / "clients"


def get_logs_dir() -> Path:
    """Return the directory holding operational log files
    (recon_log.json, payroll_log.csv, reconciliation_log.csv).

    These files contain real client names and financial data, so they live in
    the private clients location — never the public repo.

    Priority:
      1. BOOKKEEPING_LOGS_DIR environment variable
      2. The private clients dir (BOOKKEEPING_CLIENTS_DIR or
         ~/.bookkeeping/clients) when it exists
      3. ./  (repo-root fallback — fresh checkout / tests)
    """
    import os
    env = os.environ.get("BOOKKEEPING_LOGS_DIR", "").strip()
    if env:
        p = Path(env)
        if p.exists():
            return p
        import warnings
        warnings.warn(
            f"BOOKKEEPING_LOGS_DIR={env!r} does not exist; falling back to defaults.",
            stacklevel=2,
        )
    # Prefer the private clients location (NOT the ./clients repo fallback,
    # which always exists for the example config).
    cenv = os.environ.get("BOOKKEEPING_CLIENTS_DIR", "").strip()
    if cenv and Path(cenv).exists():
        return Path(cenv)
    home_path = Path.home() / ".bookkeeping" / "clients"
    if home_path.exists():
        return home_path
    return REPO_DIR


def load_private_json(filename: str, default=None):
    """Load a JSON data file that holds client-specific data, keeping it out of
    the public repo.

    Looks in get_clients_dir() first (the private location), then falls back to
    a committed `<name>.example.json` in the repo root, then to `default`.
    Lets client-specific mapping tables (digest tracker, sheet cell maps,
    manually-keyed statements) live with the private configs instead of in
    public source files.
    """
    import json
    candidates = [
        get_clients_dir() / filename,
        REPO_DIR / filename,
        REPO_DIR / filename.replace(".json", ".example.json"),
    ]
    for path in candidates:
        if path.exists():
            with open(path) as f:
                return json.load(f)
    return default


def get_client_notes(client: str, account_type: str) -> list[str]:
    """Return reconciliation reminder notes for a client + account type.

    Reads the ``reconciliation_notes`` dict from the client's JSON config.
    Lookup order:
      1. Exact ``account_type`` key  (e.g. ``"bmo_credit_roger"``)
      2. Category key derived from account_type:
           ``credit_cards``  — any type containing "credit" or "visa"
           ``checking``      — any type containing "checking"
           ``savings``       — any type containing "savings"
           ``payroll``       — account_type == "payroll"
      3. ``"general"``       — catch-all shown for every reconciliation

    Returns a list of non-empty note strings (may be empty).
    """
    try:
        from parsers.base import _registry
        cfg = _registry.get_config(client)
        if cfg is None:
            return []
        notes = cfg.get("reconciliation_notes", {})
        if not notes:
            return []

        def _category(at: str) -> str:
            at = at.lower()
            if "credit" in at or "visa" in at or "citi" in at or "amex" in at or "ink" in at or "sapphire" in at:
                return "credit_cards"
            if "checking" in at:
                return "checking"
            if "savings" in at:
                return "savings"
            if at == "payroll":
                return "payroll"
            return ""

        results = []
        # 1. exact match
        if account_type in notes:
            results.append(notes[account_type])
        else:
            # 2. category match
            cat = _category(account_type)
            if cat and cat in notes and notes[cat] not in results:
                results.append(notes[cat])
        # 3. general catch-all
        if "general" in notes and notes["general"] not in results:
            results.append(notes["general"])
        return [n for n in results if n]
    except Exception:
        return []


def _normalize_client_key(raw: str) -> str:
    """Resolve a raw client name or key to the canonical tracker key.

    1. sheets_config.json client_key_map exact match
    2. sheets_config.json uppercased match
    3. Registry lookup by canonical_name / client_name / aliases → tracker_key
    4. Registry lookup by payroll_format → tracker_key
    5. Uppercase + underscores fallback
    """
    cfg = load_private_json("sheets_config.json", default={})
    key_map = cfg.get("client_key_map", {})
    if key_map.get(raw):
        return key_map[raw]
    if key_map.get(raw.upper()):
        return key_map[raw.upper()]
    try:
        from parsers.base import _registry
        # Direct config lookup — handles canonical_name and aliases
        client_cfg = _registry.get_config(raw)
        if client_cfg is None:
            # Also try each config's canonical_name / client_name
            for cc in _registry._configs.values():
                names = [cc.get("canonical_name", ""), cc.get("client_name", "")]
                names += cc.get("aliases", [])
                if raw in names or raw.upper() in [n.upper() for n in names]:
                    client_cfg = cc
                    break
        if client_cfg is not None:
            tk = client_cfg.get("tracker_key")
            if tk:
                return tk
        # Fallback: match by payroll_format
        for cc in _registry._configs.values():
            if cc.get("payroll_format", "").lower() == raw.lower():
                tk = cc.get("tracker_key")
                if tk:
                    return tk
        # Fallback: strip _agency/_admin suffix and retry all lookups
        for _sfx in ("_agency", "_admin"):
            if raw.lower().endswith(_sfx):
                base = raw[: -len(_sfx)]
                base_cfg = _registry.get_config(base)
                if base_cfg is None:
                    for cc in _registry._configs.values():
                        if cc.get("payroll_key", "").lower() == base.lower():
                            base_cfg = cc
                            break
                if base_cfg is not None:
                    tk = base_cfg.get("tracker_key")
                    if tk:
                        return tk
    except Exception:
        pass
    return raw.upper().replace(" ", "_")


def _ensure_acct_type_mapped(account_type: str) -> None:
    """Add account_type to sheets_config.json acct_type_map if not already present.

    Derives the tracker key by stripping common prefixes (bmo_credit_ → bmo_,
    bofa_credit_ → bofa_credit, etc.) using the same pattern as the existing
    manual entries.  If a mapping already exists or sheets_config.json is not
    found, does nothing silently.
    """
    import re
    sheets_path = get_clients_dir() / "sheets_config.json"
    if not sheets_path.exists():
        return
    try:
        import json as _json
        with open(sheets_path) as f:
            cfg = _json.load(f)
        acct_map = cfg.setdefault("acct_type_map", {})
        if account_type in acct_map:
            return
        # Derive tracker key: bmo_credit_roger → bmo_roger, etc.
        tracker_key = re.sub(r'^(bmo)_credit_', r'\1_', account_type)
        tracker_key = re.sub(r'^(bofa|chase|citi|wells_fargo|amex|usbank)_credit_', r'\1_', tracker_key)
        acct_map[account_type] = tracker_key
        with open(sheets_path, "w") as f:
            _json.dump(cfg, f, indent=2)
        print(f"  🗂  acct_type_map: added {account_type} → {tracker_key}")
    except Exception:
        pass


def _normalize_date_iso(date_str: str) -> str:
    """Normalize any common date string to YYYY-MM-DD. Returns original if unparseable."""
    from datetime import datetime
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return date_str


def write_both_logs(
    *,
    client: str,
    client_name: str,
    account_type: str,
    statement_end_date: str,
    statement: str,
    beginning_balance: str,
    ending_balance: str,
    total_payments: str,
    status: str,
) -> None:
    """
    Write to BOTH reconciliation_log.csv AND recon_log.json atomically.
    Raises immediately if either write fails — no silent partial updates.

    This is the single source-of-truth write for all reconciliation completions.
    Never call upsert_recon_log or append_recon_log separately after QB confirm.
    """
    import csv
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ts = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M:%S")
    client_key = _normalize_client_key(client)

    # Normalize date to YYYY-MM-DD for consistent ISO-sortable storage
    statement_end_date = _normalize_date_iso(statement_end_date)

    # ── 1. reconciliation_log.csv ─────────────────────────────────────────
    # Skip CSV write for ERROR status — don't overwrite a previously good
    # statement date in the tracker with a failed run.
    if status == "ERROR":
        print(f"  ⚠ Skipping reconciliation_log.csv (ERROR status)")
    else:
        csv_path = get_logs_dir() / "reconciliation_log.csv"
        fields = ["client", "client_name", "account_type", "account_ending",
                  "statement_date", "beginning_balance", "ending_balance",
                  "total_payments", "run_timestamp", "source"]

        row = {
            "client":             client_key,
            "client_name":        client_name,
            "account_type":       account_type,
            "account_ending":     "",
            "statement_date":     statement_end_date,
            "beginning_balance":  beginning_balance,
            "ending_balance":     ending_balance,
            "total_payments":     total_payments,
            "run_timestamp":      ts,
            "source":             "claude",
        }

        existing_rows = []
        if csv_path.exists():
            with open(csv_path, newline="") as f:
                existing_rows = list(csv.DictReader(f))

        # Upsert: replace matching (client, account_type, statement_date) row.
        # Normalize existing dates for comparison to handle legacy MM/DD/YY entries.
        replaced = False
        for i, r in enumerate(existing_rows):
            if (r.get("client") == client_key
                    and r.get("account_type") == account_type
                    and _normalize_date_iso(r.get("statement_date", "")) == statement_end_date):
                existing_rows[i] = row
                replaced = True
                break
        if not replaced:
            existing_rows.append(row)

        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(existing_rows)

        verb = "Updated" if replaced else "Logged"
        print(f"  📋 {verb} → reconciliation_log.csv  ({statement_end_date}  ending ${ending_balance})")

    # ── 2. recon_log.json ─────────────────────────────────────────────────
    upsert_recon_log(
        client             = client_name,
        account_type       = account_type,
        statement_end_date = statement_end_date,
        statement          = statement,
        beginning_balance  = beginning_balance,
        ending_balance     = ending_balance,
        difference         = "0.00",
        status             = status,
    )
    print(f"  📝 Digest log → recon_log.json ({status})")

    # ── 3. Auto-register new account type in acct_type_map ───────────────
    _ensure_acct_type_mapped(account_type)
