#!/usr/bin/env python3
"""
tools/add_fixture_notes.py — patch client configs with Drive fixture file info.

Run once from your bookkeeping-engine directory:
    python3 tools/add_fixture_notes.py

Reads client JSON files from BOOKKEEPING_CLIENTS_DIR (or ~/Bookkeeping-clients),
adds a 'drive_test_fixtures' block and reconciliation_notes entries pointing to
the canonical fixture file names in Google Drive.
"""

import json
import os
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))

FIXTURES: dict[str, dict] = {
    "fcba_academy.json": {
        "drive_test_fixtures": {
            "amex_credit": {
                "filename": "fixture_amex_credit_fcba.pdf",
                "drive_id":  "1evC-tqzVit5iu3iZeKCvEjifCQvk17gg",
                "account":   "AMEX Business Platinum credit — account ending 2-72009",
                "note":      "Pay-over-time format; has both Pay In Full and Pay Over Time portions",
            }
        },
        "reconciliation_notes": {
            "amex": "Drive fixture: fixture_amex_credit_fcba.pdf (ID 1evC-tqzVit5iu3iZeKCvEjifCQvk17gg)",
        },
    },
    "duran_hcp.json": {
        "drive_test_fixtures": {
            "amex_credit": {
                "filename": "fixture_amex_credit_duran.pdf",
                "drive_id":  "1trjRp5pE-7EjHRad1qLPSu3Qdp-KAv75",
                "account":   "AMEX Business Platinum credit — account ending 3-51006",
                "note":      "Pay-over-time format with interest charges",
            }
        },
        "reconciliation_notes": {
            "amex": "Drive fixture: fixture_amex_credit_duran.pdf (ID 1trjRp5pE-7EjHRad1qLPSu3Qdp-KAv75)",
        },
    },
    "mp_cheng.json": {
        "drive_test_fixtures": {
            "citi_bundle": {
                "filename": "fixture_citi_bundle_mp_cheng.pdf",
                "drive_id":  "1ip0iwyf0X_JNQTcxDx8Df5mxhuNxGzb7",
                "account":   "Citi bundle — checking 205595366 + savings 205595374",
                "note":      "Single PDF contains both checking and savings; pipeline splits them",
            },
            "chase_sapphire": {
                "filename": "fixture_chase_sapphire_mp_cheng.pdf",
                "drive_id":  "1O2qt2vK4CkROhIGNNPyPLmUw8V6javpm",
                "account":   "Chase Sapphire Preferred credit — account ending 2721",
                "note":      "AutoPay on; closing date 06/06/26",
            },
        },
        "reconciliation_notes": {
            "citi_checking": "Drive fixture: fixture_citi_bundle_mp_cheng.pdf — note: file contains BOTH checking and savings",
            "citi_savings":  "Drive fixture: fixture_citi_bundle_mp_cheng.pdf — same file as citi_checking",
            "chase_sapphire": "Drive fixture: fixture_chase_sapphire_mp_cheng.pdf (ID 1O2qt2vK4CkROhIGNNPyPLmUw8V6javpm)",
        },
    },
}

ALSO_RENAME_IN_DRIVE = """
Rename these 4 files in Google Drive (right-click → Rename):
  fixture_amex_fcba.pdf       →  fixture_amex_credit_fcba.pdf
  fixture_amex_duran.pdf      →  fixture_amex_credit_duran.pdf
  fixture_citi_savings.pdf    →  fixture_citi_bundle_mp_cheng.pdf
  fixture_chase_sapphire.pdf  →  fixture_chase_sapphire_mp_cheng.pdf

Delete these test artifacts (10 files):
  test_size.pdf  (7 copies)
  fixture_test_100k.pdf
  test_tiny.txt
"""


def get_clients_dir() -> Path:
    env = os.environ.get("BOOKKEEPING_CLIENTS_DIR", "").strip()
    if env and Path(env).exists():
        return Path(env)
    home = Path.home() / "Bookkeeping-clients"
    if home.exists():
        return home
    home2 = Path.home() / ".bookkeeping" / "clients"
    if home2.exists():
        return home2
    return REPO_DIR / "clients"


def patch(clients_dir: Path) -> None:
    changed = []
    for filename, patches in FIXTURES.items():
        path = clients_dir / filename
        if not path.exists():
            print(f"  ⚠ Not found: {path} — skipping")
            continue
        with open(path) as f:
            config = json.load(f)

        # Merge drive_test_fixtures
        if "drive_test_fixtures" in patches:
            existing = config.get("drive_test_fixtures", {})
            existing.update(patches["drive_test_fixtures"])
            config["drive_test_fixtures"] = existing

        # Merge reconciliation_notes (don't overwrite existing keys)
        if "reconciliation_notes" in patches:
            existing_notes = config.get("reconciliation_notes", {})
            for key, val in patches["reconciliation_notes"].items():
                if key not in existing_notes:
                    existing_notes[key] = val
            config["reconciliation_notes"] = existing_notes

        with open(path, "w") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
        print(f"  ✅ Updated {filename}")
        changed.append(filename)

    if changed:
        print(f"\nPatched {len(changed)} client config(s).")
        print("\nNext: commit and push Bookkeeping-clients:")
        print("  cd ~/Bookkeeping-clients && git add . && git commit -m 'Add Drive fixture notes to client configs' && git push")
    print(ALSO_RENAME_IN_DRIVE)


if __name__ == "__main__":
    clients_dir = get_clients_dir()
    print(f"Clients dir: {clients_dir}\n")
    patch(clients_dir)
