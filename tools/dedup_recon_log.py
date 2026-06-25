#!/usr/bin/env python3
"""
tools/dedup_recon_log.py — remove duplicate entries from recon_log.json.

Duplicates arise when the same statement is reconciled multiple times (e.g.
test runs) or when date-format differences caused the upsert key to miss.

De-dup rule:
  For each (client, account_type, normalized_statement_end_date) group:
    - If any entry is DONE, keep the most recent DONE and drop all others.
    - If all are IN_PROGRESS, keep the most recent and drop the rest.
    - Entries with a blank statement_end_date are grouped by (client, account_type).

Usage:
    python3 tools/dedup_recon_log.py           # dry-run (shows what would change)
    python3 tools/dedup_recon_log.py --apply   # apply the dedup
"""

import json
import sys
from pathlib import Path
from datetime import datetime

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))

from log_utils import get_logs_dir, _normalize_date_iso


def _group_key(entry: dict) -> tuple:
    raw_date = entry.get("statement_end_date", "")
    norm_date = _normalize_date_iso(raw_date) if raw_date else ""
    return (
        entry.get("client", ""),
        entry.get("account_type", ""),
        norm_date,
    )


def _run_time_sort_key(entry: dict) -> str:
    return entry.get("run_time", "") or ""


def dedup(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (kept, removed) lists."""
    from collections import defaultdict
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for e in entries:
        if e.get("type") != "recon":
            continue
        groups[_group_key(e)].append(e)

    non_recon = [e for e in entries if e.get("type") != "recon"]
    kept, removed = [], []

    for key, group in groups.items():
        if len(group) == 1:
            kept.append(group[0])
            continue

        done    = [e for e in group if e.get("status") == "DONE"]
        pending = [e for e in group if e.get("status") != "DONE"]

        if done:
            winner = sorted(done,    key=_run_time_sort_key, reverse=True)[0]
            losers = [e for e in group if e is not winner]
        else:
            winner = sorted(pending, key=_run_time_sort_key, reverse=True)[0]
            losers = [e for e in group if e is not winner]

        kept.append(winner)
        removed.extend(losers)

    # Preserve original ordering of kept entries
    kept_set = {id(e) for e in kept}
    ordered_kept = [e for e in entries if id(e) in kept_set or e.get("type") != "recon"]
    return ordered_kept, removed


def main():
    apply = "--apply" in sys.argv
    log_path = get_logs_dir() / "recon_log.json"
    if not log_path.exists():
        print(f"No recon_log.json found at {log_path}")
        sys.exit(0)

    with open(log_path) as f:
        entries = json.load(f)

    kept, removed = dedup(entries)

    if not removed:
        print("recon_log.json: no duplicates found.")
        sys.exit(0)

    print(f"Found {len(removed)} duplicate(s) to remove:\n")
    for e in removed:
        print(f"  - {e.get('client')}  {e.get('account_type')}  "
              f"{e.get('statement_end_date') or '(no date)'}  "
              f"status={e.get('status')}  run_time={e.get('run_time', '')[:19]}")

    print(f"\n{len(kept)} entries would remain.")

    if not apply:
        print("\nDry run — no changes made. Re-run with --apply to remove duplicates.")
        sys.exit(0)

    with open(log_path, "w") as f:
        json.dump(kept, f, indent=2)
    print(f"\n✅ recon_log.json updated — {len(removed)} duplicate(s) removed.")


if __name__ == "__main__":
    main()
