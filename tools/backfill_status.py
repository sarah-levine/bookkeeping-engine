#!/usr/bin/env python3
"""
tools/backfill_status.py
------------------------
Rename a status value in recon_log.json.

Usage:
    python3 tools/backfill_status.py <old_status> <new_status>

Example:
    python3 tools/backfill_status.py CLEAN DONE

Reads from and writes to the logs dir (BOOKKEEPING_LOGS_DIR or
BOOKKEEPING_CLIENTS_DIR or ~/.bookkeeping/clients/).
"""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from log_utils import get_logs_dir


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)

    old, new = sys.argv[1], sys.argv[2]
    log_path = get_logs_dir() / "recon_log.json"

    if not log_path.exists():
        print(f"No recon_log.json found at {log_path}")
        sys.exit(0)

    with open(log_path) as f:
        entries = json.load(f)

    updated = 0
    for e in entries:
        if e.get("status") == old:
            e["status"] = new
            updated += 1

    with open(log_path, "w") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"  ✅ {updated} entr{'y' if updated == 1 else 'ies'} updated: {old} → {new}  ({log_path})")


if __name__ == "__main__":
    main()
