#!/usr/bin/env python3
"""
check_all.py — Run all checks before pushing to main.

Usage:
    python3 tests/check_all.py          # run all checks
    python3 tests/check_all.py --quick  # skip slow fixture smoke tests

Checks:
  1. PII scan (client name blocklist + ALLCAPS names + emails + account numbers)
  2. Unit tests (log pipeline, config, parsers)
  3. Client name normalization (all names in recon_log.json resolve)
  4. Vendor normalization (global rules load, two-tier works)
  5. Drive archiver import (dry-run, no uploads)
  6. MCP server tools load
  7. Fixture smoke tests (--dry-run against all manifested PDFs) [skipped with --quick]
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
CLIENTS_DIR = os.environ.get("BOOKKEEPING_CLIENTS_DIR",
                              str(Path.home() / "Bookkeeping-clients"))
os.environ["BOOKKEEPING_CLIENTS_DIR"] = CLIENTS_DIR

sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0


def check(name, fn):
    global PASS, FAIL
    try:
        result = fn()
        if result is True or result is None:
            print(f"  ✓  {name}")
            PASS += 1
        else:
            print(f"  ✗  {name}: {result}")
            FAIL += 1
    except Exception as e:
        print(f"  ✗  {name}: {e}")
        FAIL += 1


# ── 1. PII scan ──────────────────────────────────────────────────────────

def check_pii():
    r = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "pii_scan.py")],
        capture_output=True, text=True, env={**os.environ, "BOOKKEEPING_CLIENTS_DIR": CLIENTS_DIR}
    )
    if r.returncode != 0:
        return r.stdout.strip()
    return True


# ── 2. Unit tests ────────────────────────────────────────────────────────

def check_unit_tests():
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_log_pipeline.py",
         "tests/test_config_and_logs.py", "-x", "-q", "--tb=line"],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    if r.returncode != 0:
        last_lines = [l for l in r.stdout.strip().splitlines() if l.strip()]
        return last_lines[-1] if last_lines else "tests failed"
    return True


# ── 3. Client name normalization ─────────────────────────────────────────

def check_client_normalization():
    import json
    from log_utils import _normalize_client_name

    log_path = Path(CLIENTS_DIR) / "recon_log.json"
    if not log_path.exists():
        return True  # no log to check

    entries = json.loads(log_path.read_text())
    unresolved = set()
    for e in entries:
        c = e.get("client", "")
        if not c:
            continue
        n = _normalize_client_name(c)
        # Canonical names should be unchanged after normalization
        if n != c:
            unresolved.add(f"{c} -> {n}")

    if unresolved:
        return f"{len(unresolved)} un-normalized names: {list(unresolved)[:3]}"
    return True


# ── 4. Vendor normalization ──────────────────────────────────────────────

def check_vendor_normalization():
    from parsers.base import _registry

    # Global rules loaded?
    if not _registry._global_vendor_rules:
        return "no global vendor rules loaded"

    # Two-tier works?
    result = _registry.normalize_vendor("__NONEXISTENT__", "AMAZON.COM ORDER")
    if result == "AMAZON.COM ORDER":
        return "global Amazon rule didn't fire"

    return True


# ── 5. Drive archiver ────────────────────────────────────────────────────

def check_drive_archiver():
    from drive_archiver import STATEMENTS_ROOT, FIXTURES_ROOT
    if not STATEMENTS_ROOT:
        return "drive_statements_folder not configured"
    if not FIXTURES_ROOT:
        return "drive_fixtures_folder not configured"
    return True


# ── 6. MCP server ────────────────────────────────────────────────────────

def check_mcp_server():
    import mcp_server
    tools = [t.name for t in mcp_server.mcp._tool_manager._tools.values()]
    expected = {"reconcile", "reconcile_from_drive", "check_status",
                "mark_done", "open_issues", "client_list"}
    missing = expected - set(tools)
    if missing:
        return f"missing tools: {missing}"
    return True


# ── 7. Fixture smoke tests ───────────────────────────────────────────────

def check_fixtures():
    r = subprocess.run(
        [sys.executable, str(ROOT / "tests" / "smoke_all_fixtures.py")],
        capture_output=True, text=True, timeout=600,
        env={**os.environ, "BOOKKEEPING_CLIENTS_DIR": CLIENTS_DIR},
    )
    if r.returncode != 0:
        fail_lines = [l for l in r.stdout.splitlines() if "FAIL" in l]
        return fail_lines[0] if fail_lines else "fixture smoke tests failed"
    return True


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    quick = "--quick" in sys.argv
    print("Running pre-push checks...\n")

    check("PII scan", check_pii)
    check("Unit tests", check_unit_tests)
    check("Client name normalization", check_client_normalization)
    check("Vendor normalization (two-tier)", check_vendor_normalization)
    check("Drive archiver config", check_drive_archiver)
    check("MCP server tools", check_mcp_server)

    if not quick:
        check("Fixture smoke tests", check_fixtures)
    else:
        print("  ⊘  Fixture smoke tests (skipped — --quick)")

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL > 0 else 0)


if __name__ == "__main__":
    main()
