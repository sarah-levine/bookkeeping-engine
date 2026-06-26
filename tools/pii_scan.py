#!/usr/bin/env python3
"""
pii_scan.py — allowlist-based leak tripwire for this (public) repo.

Philosophy: a blocklist ("remove names we know") can't catch the long tail of
real third-party names that show up in transaction examples. So this flags
*every* proper-noun-looking token, every account-number pattern, and every
non-approved email, then subtracts an explicit allowlist of known-generic
tokens (banks, processors, gov agencies, national vendors, adopted
placeholders, code keywords). Anything left is surfaced for review.

Usage:
    python3 tools/pii_scan.py            # gate: ALLCAPS names in code/config
    python3 tools/pii_scan.py --staged   # gate on git-staged files (pre-commit)
    python3 tools/pii_scan.py --audit    # max recall: every file, every name
                                         #   shape (Titlecase/CamelCase too).
                                         #   Run before each publish; review the
                                         #   residual by hand — it is the
                                         #   complete set of name-like tokens.
    python3 tools/pii_scan.py f1 f2 ...  # scan specific files

Exit code 0 = clean, 1 = findings (or error). Tune by editing
tools/pii_allowlist.txt — adding a token there is a conscious "this is safe"
decision that shows up in code review.
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALLOWLIST_FILE = os.path.join(ROOT, "tools", "pii_allowlist.txt")

TEXT_EXT = (".py", ".md", ".json", ".html", ".txt", ".yml", ".yaml", ".sh", ".cfg", ".ini")

# Emails whose domain is explicitly fine to keep.
APPROVED_EMAIL_DOMAINS = {
    "example.com", "example.org", "example.net",
    "example-project.iam.gserviceaccount.com",
    "anthropic.com", "claude.ai", "users.noreply.github.com", "email.com",
    "sarah-levine.com",  # project owner's own infra email (git config), not client data
}

# Name-scanning runs only on code/config, where every leak so far has lived
# (parser logic + transaction-example docstrings + client/schema JSON). Prose
# files (.md/.yml/.html) are too capitalization-noisy to gate on; they still
# get the high-precision email + account-number checks below.
NAME_SCAN_EXT = (".py", ".json")

# the scanner and its allowlist legitimately contain pattern examples / tokens
EXCLUDE_PATHS = {"tools/pii_scan.py", "tools/pii_allowlist.txt"}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# account-number shapes: masked (****1234) and quoted 4-digit literals in code
ACCT_RES = [
    re.compile(r"\*{2,}\d{3,}"),
    re.compile(r"(?<![A-Za-z0-9])\*\d{4}(?![0-9])"),
    re.compile(r"'\d{4}'"),
]
# proper-noun shapes (ALLCAPS only — Titlecase prose is too noisy to gate on)
CAPS_MULTI = re.compile(r"\b[A-Z][A-Z&]{2,}(?:\s+[A-Z&]{2,}){1,3}\b")
CAPS_SINGLE = re.compile(r"\b[A-Z]{4,}\b")

# broader shapes used only in --audit (max recall, human-reviewed): Titlecase
# phrases and InternalCaps tokens, across every text file
AUDIT_EXT = (".py", ".json", ".md", ".html", ".txt", ".csv", ".yml", ".yaml",
             ".cfg", ".ini", ".sh")
CAMEL = re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b")
TITLE_MULTI = re.compile(r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){1,4}\b")

# common stopwords so phrases like "BANK OF AMERICA" pass when their content
# words are allowlisted
STOPWORDS = {"of", "and", "the", "to", "for", "in", "on", "a", "an", "with",
             "by", "or", "at", "as", "per", "via"}


def load_allowlist():
    allow = set()
    with open(ALLOWLIST_FILE, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            allow.add(line.lower())                       # full phrase
            for w in re.split(r"[\s&/]+", line):          # and each word
                if w:
                    allow.add(w.lower())
    return allow


def tracked_text_files_any():
    return subprocess.check_output(
        ["git", "-C", ROOT, "ls-files"]).decode().split("\n")


def tracked_text_files():
    return [f for f in tracked_text_files_any() if f.endswith(TEXT_EXT)]


def staged_text_files():
    out = subprocess.check_output(
        ["git", "-C", ROOT, "diff", "--cached", "--name-only", "--diff-filter=ACM"]
    ).decode().split("\n")
    return [f for f in out if f.endswith(TEXT_EXT) and os.path.exists(os.path.join(ROOT, f))]


def phrase_ok(phrase, allow):
    """A phrase is safe if the whole phrase, or every content word, is known."""
    if phrase.lower() in allow:
        return True
    return all(w.lower() in allow or w.lower() in STOPWORDS
               for w in re.split(r"[\s&]+", phrase) if w)


def _load_client_blocklist():
    """Build a set of client-name patterns to block in public code.

    Reads client config filenames from the private clients dir and generates
    lowercase patterns: the config slug (e.g. 'acme_inc'), words from
    canonical_name, and aliases. These catch references like
    'fixture_amex_acme.pdf' or 'acme_inc.json' that slip past the
    ALLCAPS-only name scanner.
    """
    blocklist = set()
    clients_dir = os.environ.get("BOOKKEEPING_CLIENTS_DIR", "")
    if not clients_dir:
        return blocklist
    import json, pathlib
    cd = pathlib.Path(clients_dir)
    if not cd.exists():
        return blocklist
    skip = {"sheets_config.json", "sheets_credentials.json", "digest_config.json",
            "fixtures_manifest.json", "manual_statements.json", "recon_log.json",
            "vendor_rules_global.json", "drive_credentials.json", "drive_token.pickle"}
    for f in cd.glob("*.json"):
        if f.name in skip or f.name.startswith("_"):
            continue
        try:
            cfg = json.loads(f.read_text())
        except Exception:
            continue
        if not isinstance(cfg, dict) or not cfg.get("canonical_name"):
            continue
        # Add the config slug (e.g. "acme_inc")
        slug = f.stem.lower()
        blocklist.add(slug)
        # Add individual words from canonical_name (skip short ones)
        for w in re.split(r"[\s&,.']+", cfg.get("canonical_name", "")):
            if len(w) >= 4:
                blocklist.add(w.lower())
        # Add aliases
        for alias in cfg.get("aliases", []):
            for w in re.split(r"[\s&,.']+", alias):
                if len(w) >= 4:
                    blocklist.add(w.lower())
    # Remove generic words that would cause false positives
    generics = {"hair", "studio", "parts", "service", "academy", "west",
                "human", "capital", "partners", "appliance", "realty",
                "silicon", "valley", "north", "south", "east"}
    blocklist -= generics
    return blocklist


def scan_file(path, allow, audit=False, client_blocklist=None):
    findings = []
    full = os.path.join(ROOT, path)
    try:
        with open(full, "rb") as fh:
            text = fh.read().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return findings
    # gate scans names in code/config; --audit scans names in every text file
    name_scan = audit or path.endswith(NAME_SCAN_EXT)
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in EMAIL_RE.findall(line):
            dom = m.split("@", 1)[1].lower()
            if dom not in APPROVED_EMAIL_DOMAINS:
                findings.append((lineno, "email", m))
        for rx in ACCT_RES:
            for m in rx.findall(line):
                findings.append((lineno, "account#", m))
        if not name_scan:
            continue
        for m in CAPS_MULTI.findall(line):
            if not phrase_ok(m, allow):
                findings.append((lineno, "name?", m))
        for m in CAPS_SINGLE.findall(line):
            if m.lower() not in allow and m.lower() not in STOPWORDS:
                findings.append((lineno, "name?", m))
        if not audit:
            # Client-name blocklist: catch lowercase/underscore references
            if client_blocklist and name_scan:
                line_lower = line.lower()
                for blocked in client_blocklist:
                    if blocked in line_lower:
                        findings.append((lineno, "client?", blocked))
            continue
        # max-recall extra shapes: Titlecase phrases + InternalCaps tokens
        for m in TITLE_MULTI.findall(line):
            if not phrase_ok(m, allow):
                findings.append((lineno, "name?", m))
        for m in CAMEL.findall(line):
            if m.lower() not in allow:
                findings.append((lineno, "name?", m))
        # Client-name blocklist (audit mode too)
        if client_blocklist:
            line_lower = line.lower()
            for blocked in client_blocklist:
                if blocked in line_lower:
                    findings.append((lineno, "client?", blocked))
    return findings


def main():
    args = [a for a in sys.argv[1:]]
    audit = "--audit" in args
    allow = load_allowlist()
    if "--staged" in args:
        files = staged_text_files()
    elif args and not args[0].startswith("-"):
        files = [a for a in args if not a.startswith("-")]
    elif audit:
        files = [f for f in tracked_text_files_any() if f.endswith(AUDIT_EXT)]
    else:
        files = tracked_text_files()

    client_blocklist = _load_client_blocklist()
    total = 0
    for f in files:
        if f in EXCLUDE_PATHS:
            continue
        hits = scan_file(f, allow, audit=audit, client_blocklist=client_blocklist)
        # de-dupe identical (type,value) per file to keep output readable
        seen = set()
        for lineno, kind, val in hits:
            key = (kind, val)
            if key in seen:
                continue
            seen.add(key)
            print(f"{f}:{lineno}: [{kind}] {val}")
            total += 1
    if total:
        print(f"\npii_scan: {total} finding(s). If a token is genuinely "
              f"generic/fictional, add it to tools/pii_allowlist.txt; "
              f"otherwise scrub it before committing.", file=sys.stderr)
        return 1
    print("pii_scan: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
