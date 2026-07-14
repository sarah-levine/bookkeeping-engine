"""
dump_report.py
---------------
Report-diff verification harness for parser refactors. Runs a real fixture
through its parser, prints generate_report()'s output with the
`Generated: <timestamp>` line normalized (so two runs a second apart still
compare equal), and optionally diffs against a saved "before" snapshot.

This does NOT exist as an automated check anywhere else in the repo —
REFACTORING_ROADMAP.md's testing policy requires a full before/after report
diff against every real fixture a parser has, but historically that's been
done by hand (git stash + manual diff). This script makes that repeatable.

Usage:
    # Print a fixture's report to stdout (timestamp normalized)
    python3 tests/dump_report.py northern_trust_checking_needles

    # Save a "before" snapshot
    python3 tests/dump_report.py northern_trust_checking_needles --out /tmp/before.txt

    # After a code change, diff against the saved snapshot
    python3 tests/dump_report.py northern_trust_checking_needles --compare-to /tmp/before.txt

Reuses tests/test_parsers.py's PARSER_MAP/load_manifest() and
tests/drive_fixtures.py's fetch_pdf_entry() — one source of truth for
"format string -> parser class" and fixture retrieval, not duplicated here.
"""

import argparse
import difflib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.test_parsers import PARSER_MAP, load_manifest  # noqa: E402
from tests.drive_fixtures import fetch_pdf_entry, DriveUnavailable  # noqa: E402

_TIMESTAMP_RE = re.compile(r'^Generated: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$', re.MULTILINE)
_TIMESTAMP_PLACEHOLDER = 'Generated: <TIMESTAMP>'


def _normalize(report_text: str) -> str:
    return _TIMESTAMP_RE.sub(_TIMESTAMP_PLACEHOLDER, report_text)


def _find_entry(name: str) -> dict:
    manifest, path = load_manifest()
    for entry in manifest.get("statements", []):
        if entry["name"] == name:
            return entry
    raise SystemExit(f"No fixture named {name!r} in {path}")


def dump(name: str) -> str:
    """Parse the named fixture and return its normalized report text."""
    entry = _find_entry(name)
    fmt = entry["format"]
    parser_cls = PARSER_MAP.get(fmt)
    if not parser_cls:
        raise SystemExit(f"Unknown format {fmt!r} for fixture {name!r}")

    pdf = fetch_pdf_entry(entry, cache_name=f"{entry['name']}.pdf")
    parser = parser_cls(str(pdf))
    parser.parse()
    report = parser.generate_report()
    return _normalize(report)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", help="fixture name, as it appears in fixtures_manifest.json")
    ap.add_argument("--out", metavar="PATH", help="write the normalized report to PATH instead of stdout")
    ap.add_argument("--compare-to", metavar="PATH",
                     help="diff the current report against a saved snapshot; exits 1 on any difference")
    args = ap.parse_args()

    try:
        report = dump(args.name)
    except DriveUnavailable as e:
        print(f"SKIP {args.name}: Drive unavailable ({e})", file=sys.stderr)
        return 1

    if args.compare_to:
        before = _normalize(Path(args.compare_to).read_text())
        if before == report:
            print(f"MATCH  {args.name}: report is byte-identical to {args.compare_to}")
            return 0
        diff = difflib.unified_diff(
            before.splitlines(keepends=True),
            report.splitlines(keepends=True),
            fromfile=args.compare_to,
            tofile=f"{args.name} (current)",
        )
        sys.stdout.writelines(diff)
        print(f"\nDIFF  {args.name}: report differs from {args.compare_to}", file=sys.stderr)
        return 1

    if args.out:
        Path(args.out).write_text(report)
    else:
        sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
